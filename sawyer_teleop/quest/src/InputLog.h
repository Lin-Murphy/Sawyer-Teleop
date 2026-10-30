#pragma once
#include <android/log.h>
#include <stdexcept>
#include <string>

inline void writeInputPacket(const std::string& packet) {
    // Leave room for priority, tag and terminators in Android's 4068-byte entry.
    if (packet.size() > 4000) {
        throw std::length_error("Quest input packet exceeds log entry limit");
    }
    // __android_log_print has a 1024-byte formatting buffer and truncates JSON.
    __android_log_write(ANDROID_LOG_INFO, "SawyerQuestV3", packet.c_str());
}
