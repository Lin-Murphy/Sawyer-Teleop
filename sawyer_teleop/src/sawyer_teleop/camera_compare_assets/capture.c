/* Read existing V4L2 format/controls, then acquire frames without setting parameters.
 * REQBUFS provides the kernel ownership gate: an existing owner causes EBUSY.
 * Continuous snapshots are atomically renamed and contain five uint64_t fields
 * (sequence, receive-monotonic-ns, receive-UTC-ns, device-timestamp-ns, flags)
 * followed by the unchanged native frame bytes. No cross-host clock comparison.
 */
#define _POSIX_C_SOURCE 200809L
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <stdint.h>

static volatile sig_atomic_t stopping;
static void on_stop(int sig) { (void)sig; stopping = 1; }
static void fail(const char *message) { perror(message); exit(2); }
static int64_t now_ns(clockid_t clock) {
    struct timespec t;
    clock_gettime(clock, &t);
    return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void read_controls(int fd, const char *path) {
    FILE *file = fopen(path, "w");
    if (!file) fail("controls file");
    struct v4l2_queryctrl query = {.id = V4L2_CTRL_FLAG_NEXT_CTRL};
    while (ioctl(fd, VIDIOC_QUERYCTRL, &query) == 0) {
        if (!(query.flags & V4L2_CTRL_FLAG_DISABLED) && query.type != V4L2_CTRL_TYPE_CTRL_CLASS) {
            struct v4l2_control control = {.id = query.id};
            if (ioctl(fd, VIDIOC_G_CTRL, &control) == 0)
                fprintf(file, "%u\t%s\t%d\n", query.id, query.name, control.value);
        }
        query.id |= V4L2_CTRL_FLAG_NEXT_CTRL;
    }
    fclose(file);
}

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) return 2;
    int continuous = argc == 4 && strcmp(argv[3], "--continuous") == 0;
    signal(SIGINT, on_stop);
    signal(SIGTERM, on_stop);
    int fd = open(argv[1], O_RDWR | O_NONBLOCK);
    if (fd < 0) fail("open");
    char path[4096], target[4096];
    snprintf(path, sizeof(path), "%s/controls_before.tsv", argv[2]);
    read_controls(fd, path);
    struct v4l2_capability cap = {0};
    if (ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) fail("QUERYCAP");
    struct v4l2_format format = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE};
    if (ioctl(fd, VIDIOC_G_FMT, &format) < 0) fail("G_FMT");
    struct v4l2_streamparm parm = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE};
    ioctl(fd, VIDIOC_G_PARM, &parm);
    snprintf(path, sizeof(path), "%s/format.json", argv[2]);
    FILE *file = fopen(path, "w");
    if (!file) fail("format file");
    fprintf(file, "{\"device\":\"%s\",\"driver\":\"%s\",\"card\":\"%s\",\"bus\":\"%s\","
                  "\"width\":%u,\"height\":%u,\"fourcc\":\"%.4s\",\"bytesperline\":%u,"
                  "\"sizeimage\":%u,\"colorspace\":%u,\"ycbcr_enc\":%u,\"quantization\":%u,"
                  "\"timeperframe\":[%u,%u],\"parameter_set_calls\":0}\n",
            argv[1], cap.driver, cap.card, cap.bus_info, format.fmt.pix.width, format.fmt.pix.height,
            (char *)&format.fmt.pix.pixelformat, format.fmt.pix.bytesperline, format.fmt.pix.sizeimage,
            format.fmt.pix.colorspace, format.fmt.pix.ycbcr_enc, format.fmt.pix.quantization,
            parm.parm.capture.timeperframe.numerator, parm.parm.capture.timeperframe.denominator);
    fclose(file);
    /* No S_FMT, S_PARM, S_CTRL, S_EXT_CTRLS, reset, enable or robot interfaces. */
    struct v4l2_requestbuffers request = {
        .count = 4, .type = V4L2_BUF_TYPE_VIDEO_CAPTURE, .memory = V4L2_MEMORY_MMAP
    };
    if (ioctl(fd, VIDIOC_REQBUFS, &request) < 0) fail("REQBUFS (no retry/takeover)");
    if (request.count > 16 || request.count < 2) fail("buffer count");
    void *memory[16];
    size_t lengths[16];
    for (unsigned i = 0; i < request.count; i++) {
        struct v4l2_buffer buffer = {.type = request.type, .memory = request.memory, .index = i};
        if (ioctl(fd, VIDIOC_QUERYBUF, &buffer) < 0) fail("QUERYBUF");
        lengths[i] = buffer.length;
        memory[i] = mmap(NULL, buffer.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buffer.m.offset);
        if (memory[i] == MAP_FAILED) fail("mmap");
        if (ioctl(fd, VIDIOC_QBUF, &buffer) < 0) fail("QBUF");
    }
    enum v4l2_buf_type type = request.type;
    if (ioctl(fd, VIDIOC_STREAMON, &type) < 0) fail("STREAMON");
    snprintf(path, sizeof(path), "%s/frames.jsonl", argv[2]);
    file = fopen(path, "w");
    if (!file) fail("frames file");
    int64_t deadline = now_ns(CLOCK_MONOTONIC) + 8000000000LL;
    unsigned kept = 0;
    for (unsigned i = 0; !stopping && (continuous || i < 45);) {
        fd_set set;
        FD_ZERO(&set); FD_SET(fd, &set);
        struct timeval timeout = {.tv_sec = 1};
        int ready = select(fd + 1, &set, NULL, NULL, &timeout);
        if (!continuous && now_ns(CLOCK_MONOTONIC) > deadline) fail("capture deadline");
        if (ready < 0) {
            if (errno == EINTR && stopping) break;
            fail("select");
        }
        if (!ready) continue;
        struct v4l2_buffer buffer = {.type = request.type, .memory = request.memory};
        if (ioctl(fd, VIDIOC_DQBUF, &buffer) < 0) {
            if (errno == EAGAIN) continue;
            fail("DQBUF");
        }
        int64_t monotonic = now_ns(CLOCK_MONOTONIC), utc = now_ns(CLOCK_REALTIME);
        if (!(buffer.flags & V4L2_BUF_FLAG_ERROR) && (continuous || i >= 40)) {
            if (continuous) snprintf(path, sizeof(path), "%s/current.raw.tmp", argv[2]);
            else snprintf(path, sizeof(path), "%s/frame_%02u.raw", argv[2], i);
            FILE *raw = fopen(path, "wb");
            if (!raw) fail("raw file");
            if (continuous) {
                uint64_t header[5] = {buffer.sequence, monotonic, utc,
                    (uint64_t)buffer.timestamp.tv_sec * 1000000000 + buffer.timestamp.tv_usec * 1000,
                    buffer.flags};
                if (fwrite(header, sizeof(header), 1, raw) != 1) fail("write header");
            }
            if (fwrite(memory[buffer.index], 1, buffer.bytesused, raw) != buffer.bytesused) fail("write raw");
            if (fclose(raw)) fail("close raw");
            if (continuous) {
                snprintf(target, sizeof(target), "%s/current.raw", argv[2]);
                if (rename(path, target)) fail("rename raw");
            } else {
                fprintf(file, "{\"frame\":%u,\"sequence\":%u,\"bytes\":%u,"
                              "\"device_timestamp_sec\":%ld,\"device_timestamp_usec\":%ld,\"flags\":%u,"
                              "\"received_monotonic_ns\":%lld,\"received_utc_ns\":%lld,\"raw_path\":\"%s\"}\n",
                        i, buffer.sequence, buffer.bytesused, buffer.timestamp.tv_sec,
                        buffer.timestamp.tv_usec, buffer.flags, (long long)monotonic, (long long)utc, path);
                fflush(file);
            }
            kept++;
        }
        i++;
        if (ioctl(fd, VIDIOC_QBUF, &buffer) < 0) fail("QBUF");
    }
    fclose(file);
    if (ioctl(fd, VIDIOC_STREAMOFF, &type) < 0) fail("STREAMOFF");
    for (unsigned i = 0; i < request.count; i++) munmap(memory[i], lengths[i]);
    struct v4l2_format after = {.type = type};
    if (ioctl(fd, VIDIOC_G_FMT, &after) < 0) fail("G_FMT after");
    if (after.fmt.pix.width != format.fmt.pix.width || after.fmt.pix.height != format.fmt.pix.height ||
        after.fmt.pix.pixelformat != format.fmt.pix.pixelformat) return 3;
    snprintf(path, sizeof(path), "%s/controls_after.tsv", argv[2]);
    read_controls(fd, path);
    close(fd);
    printf("captured %u frames; no camera parameter writes\n", kept);
    return 0;
}
