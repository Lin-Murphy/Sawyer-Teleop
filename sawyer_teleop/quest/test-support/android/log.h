#pragma once
enum { ANDROID_LOG_INFO = 4 };
extern "C" int __android_log_write(int, const char*, const char*);
extern "C" int __android_log_print(int, const char*, const char*, ...);
