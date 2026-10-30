// Host regression: Android's printf buffer is 1024 bytes; raw writes are not.
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <iostream>
#include "src/InputLog.h"

static std::string received;
static int writes = 0;
extern "C" int __android_log_write(int priority, const char* tag, const char* message) {
    assert(priority == ANDROID_LOG_INFO && std::string(tag) == "SawyerQuestV3");
    received = message;
    ++writes;
    return 1;
}
extern "C" int __android_log_print(int priority, const char* tag, const char* format, ...) {
    char buffer[1024];
    va_list args;
    va_start(args, format);
    vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    return __android_log_write(priority, tag, buffer);
}

int main() {
    for (size_t size : {1023u, 1024u, 1500u, 4000u}) {
        const std::string packet = "{\"payload\":\"" + std::string(size - 14, 'x') + "\"}";
        assert(packet.size() == size);
        writeInputPacket(packet);
        assert(received == packet);
        std::cout << received << '\n';
    }
    const int count = writes;
    bool rejected = false;
    try { writeInputPacket(std::string(4001, 'x')); }
    catch (const std::length_error&) { rejected = true; }
    assert(rejected && writes == count);  // Never emit part of an oversized frame.
}
