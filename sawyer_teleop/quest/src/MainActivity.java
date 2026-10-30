package com.rail.oculus.teleop;

import android.app.NativeActivity;

public final class MainActivity extends NativeActivity {
    static {
        System.loadLibrary("openxr_loader");
    }
}
