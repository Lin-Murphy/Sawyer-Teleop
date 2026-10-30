// Standalone Quest input: system passthrough, no virtual room or camera capture.
#define XR_USE_PLATFORM_ANDROID
#define XR_USE_GRAPHICS_API_OPENGL_ES
#include <jni.h>
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <android_native_app_glue.h>
#include <android/log.h>
#include <algorithm>
#include <array>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <time.h>
#include <deque>
#include "StatusPanel.h"
#include "RefreshPolicy.h"
#include "InputLog.h"

#define LOG(...) __android_log_print(ANDROID_LOG_INFO, "SawyerPassthrough", __VA_ARGS__)
static void check(XrResult r, const char* call) {
    if (XR_FAILED(r)) throw std::runtime_error(std::string(call) + ": " + std::to_string(r));
}
#define XR(call) check((call), #call)
static uint64_t bootNs() {
    timespec t{};
    clock_gettime(CLOCK_BOOTTIME, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + t.tv_nsec;
}
static void poseJson(std::ostream& out, const XrPosef& pose) {
    const auto& p = pose.position;
    const auto& q = pose.orientation;
    out << "{\"p\":[" << p.x << ',' << p.y << ',' << p.z
        << "],\"q\":[" << q.x << ',' << q.y << ',' << q.z << ',' << q.w << "]}";
}

class InputApp {
    android_app* app;
    XrInstance instance = XR_NULL_HANDLE;
    XrSession session = XR_NULL_HANDLE;
    XrSystemId system = XR_NULL_SYSTEM_ID;
    XrSpace floor = XR_NULL_HANDLE, view = XR_NULL_HANDLE;
    XrActionSet actionSet = XR_NULL_HANDLE;
    XrAction poseAction{}, gripAction{}, triggerAction{}, buttonAction{}, discardAction{}, resetAction{};
    std::array<XrPath, 2> handPaths{};
    std::array<XrSpace, 2> handSpaces{};
    XrPassthroughFB passthrough{};
    XrPassthroughLayerFB passthroughLayer{};
    PFN_xrCreatePassthroughFB createPassthrough{};
    PFN_xrDestroyPassthroughFB destroyPassthrough{};
    PFN_xrPassthroughStartFB startPassthrough{};
    PFN_xrPassthroughPauseFB pausePassthrough{};
    PFN_xrCreatePassthroughLayerFB createLayer{};
    PFN_xrDestroyPassthroughLayerFB destroyLayer{};
    PFN_xrPassthroughLayerResumeFB resumeLayer{};
    PFN_xrPassthroughLayerPauseFB pauseLayer{};
    EGLDisplay display = EGL_NO_DISPLAY;
    EGLContext context = EGL_NO_CONTEXT;
    EGLSurface surface = EGL_NO_SURFACE;
    bool running = false, focused = false, present = false;
    bool presenceSupported = false, passthroughHealthy = true;
    bool refreshSupported = false;
    PFN_xrEnumerateDisplayRefreshRatesFB enumerateRefresh{};
    PFN_xrGetDisplayRefreshRateFB getRefresh{};
    PFN_xrRequestDisplayRefreshRateFB requestRefresh{};
    float actualRefresh = 0;
    XrResult refreshResult = XR_ERROR_EXTENSION_NOT_PRESENT;
    bool quit = false;
    XrSwapchain hudSwapchain = XR_NULL_HANDLE;
    std::vector<XrSwapchainImageOpenGLESKHR> hudImages;
    bool hudReady = false, hudImageAcquired = false;
    uint32_t hudImageIndex = 0;
    uint64_t hudUpdated = 0, rateStarted = 0, rateFrames = 0;
    double inputHz = 0;
    std::array<bool, 2> handTracked{}, handActive{};
    float rightGrip = 0, rightTrigger = 0;
    bool headTracked = false;
    std::string previousInputState;
    std::deque<std::string> hudLog;

    void requestInput120() {
        actualRefresh = 0;
        rateStarted = rateFrames = 0;
        inputHz = 0;
        if (!refreshSupported) {
            note("120 HZ EXTENSION UNAVAILABLE");
            return;
        }
        uint32_t count = 0;
        refreshResult = enumerateRefresh(session, 0, &count, nullptr);
        std::vector<float> rates(count);
        if (XR_SUCCEEDED(refreshResult) && count)
            refreshResult = enumerateRefresh(session, count, &count, rates.data());
        if (XR_SUCCEEDED(refreshResult)) {
            if (supportsInput120(rates)) refreshResult = requestRefresh(session, 120.0f);
            else refreshResult = XR_ERROR_DISPLAY_REFRESH_RATE_UNSUPPORTED_FB;
        }
        float observed = 0;
        if (XR_SUCCEEDED(getRefresh(session, &observed))) actualRefresh = observed;
        LOG("INPUT120 request_result=%d actual_hz=%.3f", refreshResult, actualRefresh);
        note(XR_SUCCEEDED(refreshResult) ? "120 HZ REQUESTED" : "120 HZ UNAVAILABLE");
    }

    void updateInputRate() {
        const auto now = bootNs();
        if (!rateStarted) rateStarted = now;
        ++rateFrames;
        if (now - rateStarted >= 1000000000ULL) {
            inputHz = rateFrames * 1e9 / (now - rateStarted);
            rateFrames = 0;
            rateStarted = now;
            float observed = 0;
            if (refreshSupported && XR_SUCCEEDED(getRefresh(session, &observed))) actualRefresh = observed;
        }
    }

    void note(const std::string& message) {
        auto seconds = (bootNs() - stream) / 1000000000ULL;
        hudLog.push_front(std::to_string(seconds) + "S " + message);
        while (hudLog.size() > 3) hudLog.pop_back();
        LOG("HUD: %s", message.c_str());
    }
    void initHud() {
        uint32_t count = 0;
        XR(xrEnumerateSwapchainFormats(session, 0, &count, nullptr));
        std::vector<int64_t> formats(count);
        XR(xrEnumerateSwapchainFormats(session, count, &count, formats.data()));
        int64_t format = GL_RGBA8;
        if (std::find(formats.begin(), formats.end(), format) == formats.end()) {
            format = GL_SRGB8_ALPHA8;
            if (std::find(formats.begin(), formats.end(), format) == formats.end())
                throw std::runtime_error("No RGBA HUD swapchain format");
        }
        XrSwapchainCreateInfo info{XR_TYPE_SWAPCHAIN_CREATE_INFO};
        info.usageFlags = XR_SWAPCHAIN_USAGE_SAMPLED_BIT | XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT;
        info.format = format;
        info.sampleCount = 1;
        info.width = StatusPanel::width;
        info.height = StatusPanel::height;
        info.faceCount = info.arraySize = info.mipCount = 1;
        XR(xrCreateSwapchain(session, &info, &hudSwapchain));
        XR(xrEnumerateSwapchainImages(hudSwapchain, 0, &count, nullptr));
        hudImages.resize(count, {XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR});
        XR(xrEnumerateSwapchainImages(hudSwapchain, count, &count,
            reinterpret_cast<XrSwapchainImageBaseHeader*>(hudImages.data())));
        note("PASSTHROUGH STARTED");
    }
    void updateHud(bool usable) {
        const auto now = bootNs();
        std::string status = !usable ? "APP NOT ACTIVE" :
            !headTracked ? "HEAD TRACKING LOST" :
            !handActive[1] ? "RIGHT INPUT INACTIVE" :
            !handTracked[1] ? "RIGHT TRACKING LOST" : "INPUT OK";
        if (status != previousInputState) {
            note(status);
            previousInputState = status;
        }
        if (hudReady && now - hudUpdated < 250000000ULL) return;
        using namespace StatusPanel;
        std::ostringstream values, rate;
        values << std::fixed << std::setprecision(2) << "GRIP " << rightGrip << "  TRIGGER " << rightTrigger;
        rate << "DISPLAY " << int(actualRefresh + .5) << " INPUT " << int(inputHz + .5) << " HZ";
        std::vector<Line> lines = {
            {"SAWYER / INPUT 120 HZ", white},
            {"PASSTHROUGH " + std::string(passthroughHealthy ? "ON" : "ERROR"), passthroughHealthy ? green : amber},
            {status, status == "INPUT OK" ? green : amber},
            {std::string("L ") + (handTracked[0] ? "TRACKED" : "LOST") + "  R " +
                (handTracked[1] ? "TRACKED" : "LOST"), white},
            {values.str(), white}, {rate.str(), muted},
            {actualRefresh > 119 ? "A SAVE / B DISCARD - SEE PC" : "120 HZ NOT ACTIVE - SEE PC", amber}
        };
        for (const auto& message : hudLog) lines.push_back({message, muted});
        auto pixels = render(lines);
        XrSwapchainImageAcquireInfo acquire{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
        if (!hudImageAcquired) {
            XR(xrAcquireSwapchainImage(hudSwapchain, &acquire, &hudImageIndex));
            hudImageAcquired = true;
        }
        XrSwapchainImageWaitInfo wait{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO};
        wait.timeout = 0; // Never stall the input stream waiting for a HUD texture.
        XrResult waited = xrWaitSwapchainImage(hudSwapchain, &wait);
        if (waited == XR_TIMEOUT_EXPIRED) return; // Retry this image next frame.
        XR(waited);
        glBindTexture(GL_TEXTURE_2D, hudImages[hudImageIndex].image);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
        GLenum error = glGetError();
        glFlush();
        XrSwapchainImageReleaseInfo release{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
        XR(xrReleaseSwapchainImage(hudSwapchain, &release));
        hudImageAcquired = false;
        if (error != GL_NO_ERROR) throw std::runtime_error("HUD texture upload failed");
        hudReady = true;
        hudUpdated = now;
    }
    XrTime pendingRecenter = 0;
    uint64_t stream = bootNs(), sequence = 0, recenter = 0;

    template<class T> void proc(const char* name, T& out) {
        XR(xrGetInstanceProcAddr(instance, name, reinterpret_cast<PFN_xrVoidFunction*>(&out)));
    }
    XrPath path(const char* value) {
        XrPath result;
        XR(xrStringToPath(instance, value, &result));
        return result;
    }
    XrAction action(const char* name, XrActionType type) {
        XrActionCreateInfo info{XR_TYPE_ACTION_CREATE_INFO};
        std::strcpy(info.actionName, name);
        std::strcpy(info.localizedActionName, name);
        info.actionType = type;
        info.countSubactionPaths = handPaths.size();
        info.subactionPaths = handPaths.data();
        XrAction result;
        XR(xrCreateAction(actionSet, &info, &result));
        return result;
    }
    XrSpace reference(XrReferenceSpaceType type) {
        XrReferenceSpaceCreateInfo info{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
        info.referenceSpaceType = type;
        info.poseInReferenceSpace.orientation.w = 1;
        XrSpace result;
        XR(xrCreateReferenceSpace(session, &info, &result));
        return result;
    }
    void init() {
        JNIEnv* env = nullptr;
        app->activity->vm->AttachCurrentThread(&env, nullptr);
        PFN_xrInitializeLoaderKHR initLoader{};
        XR(xrGetInstanceProcAddr(XR_NULL_HANDLE, "xrInitializeLoaderKHR",
                               reinterpret_cast<PFN_xrVoidFunction*>(&initLoader)));
        XrLoaderInitInfoAndroidKHR loader{XR_TYPE_LOADER_INIT_INFO_ANDROID_KHR};
        loader.applicationVM = app->activity->vm;
        loader.applicationContext = app->activity->clazz;
        XR(initLoader(reinterpret_cast<XrLoaderInitInfoBaseHeaderKHR*>(&loader)));
        uint32_t count = 0;
        XR(xrEnumerateInstanceExtensionProperties(nullptr, 0, &count, nullptr));
        std::vector<XrExtensionProperties> available(count, {XR_TYPE_EXTENSION_PROPERTIES});
        XR(xrEnumerateInstanceExtensionProperties(nullptr, count, &count, available.data()));
        auto supports = [&](const char* name) {
            return std::any_of(available.begin(), available.end(), [&](const auto& e) {
                return std::strcmp(e.extensionName, name) == 0;
            });
        };
        std::vector<const char*> extensions = {XR_KHR_ANDROID_CREATE_INSTANCE_EXTENSION_NAME,
            XR_KHR_OPENGL_ES_ENABLE_EXTENSION_NAME, XR_FB_PASSTHROUGH_EXTENSION_NAME};
        for (auto name : extensions)
            if (!supports(name)) throw std::runtime_error(std::string("Missing extension: ") + name);
        presenceSupported = supports(XR_EXT_USER_PRESENCE_EXTENSION_NAME);
        if (presenceSupported) extensions.push_back(XR_EXT_USER_PRESENCE_EXTENSION_NAME);
        refreshSupported = supports(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);
        if (refreshSupported) extensions.push_back(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);
        XrInstanceCreateInfoAndroidKHR android{XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR};
        android.applicationVM = app->activity->vm;
        android.applicationActivity = app->activity->clazz;
        XrInstanceCreateInfo info{XR_TYPE_INSTANCE_CREATE_INFO};
        info.next = &android;
        std::strcpy(info.applicationInfo.applicationName, "Sawyer Quest Passthrough");
        info.applicationInfo.applicationVersion = 8;
        info.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);
        info.enabledExtensionCount = extensions.size();
        info.enabledExtensionNames = extensions.data();
        XR(xrCreateInstance(&info, &instance));
        if (refreshSupported) {
            proc("xrEnumerateDisplayRefreshRatesFB", enumerateRefresh);
            proc("xrGetDisplayRefreshRateFB", getRefresh);
            proc("xrRequestDisplayRefreshRateFB", requestRefresh);
        }
        XrSystemGetInfo systemInfo{XR_TYPE_SYSTEM_GET_INFO};
        systemInfo.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
        XR(xrGetSystem(instance, &systemInfo, &system));
        XrSystemPassthroughPropertiesFB passProps{XR_TYPE_SYSTEM_PASSTHROUGH_PROPERTIES_FB};
        XrSystemUserPresencePropertiesEXT presenceProps{XR_TYPE_SYSTEM_USER_PRESENCE_PROPERTIES_EXT};
        if (presenceSupported) passProps.next = &presenceProps;
        XrSystemProperties props{XR_TYPE_SYSTEM_PROPERTIES};
        props.next = &passProps;
        XR(xrGetSystemProperties(instance, system, &props));
        if (!passProps.supportsPassthrough) throw std::runtime_error("Passthrough unavailable");
        presenceSupported = presenceSupported && presenceProps.supportsUserPresence;
        PFN_xrGetOpenGLESGraphicsRequirementsKHR requirementsFn;
        proc("xrGetOpenGLESGraphicsRequirementsKHR", requirementsFn);
        XrGraphicsRequirementsOpenGLESKHR requirements{XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_ES_KHR};
        XR(requirementsFn(instance, system, &requirements));
        display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
        if (!eglInitialize(display, nullptr, nullptr)) throw std::runtime_error("eglInitialize");
        eglBindAPI(EGL_OPENGL_ES_API);
        const EGLint attrs[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
            EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
        EGLConfig config;
        EGLint n;
        if (!eglChooseConfig(display, attrs, &config, 1, &n) || n == 0)
            throw std::runtime_error("eglChooseConfig");
        const EGLint contextAttrs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
        context = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttrs);
        const EGLint surfaceAttrs[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
        surface = eglCreatePbufferSurface(display, config, surfaceAttrs);
        if (context == EGL_NO_CONTEXT || surface == EGL_NO_SURFACE ||
            !eglMakeCurrent(display, surface, surface, context)) throw std::runtime_error("EGL context");
        XrGraphicsBindingOpenGLESAndroidKHR graphics{XR_TYPE_GRAPHICS_BINDING_OPENGL_ES_ANDROID_KHR};
        graphics.display = display;
        graphics.config = config;
        graphics.context = context;
        XrSessionCreateInfo sessionInfo{XR_TYPE_SESSION_CREATE_INFO};
        sessionInfo.next = &graphics;
        sessionInfo.systemId = system;
        XR(xrCreateSession(instance, &sessionInfo, &session));
        // STAGE provides floor coordinates: +X right, +Y up, -Z forward.
        // Protocol space=1 is our normalized floor-space contract, not an XR enum.
        initHud();
        floor = reference(XR_REFERENCE_SPACE_TYPE_STAGE);
        view = reference(XR_REFERENCE_SPACE_TYPE_VIEW);
        handPaths = {path("/user/hand/left"), path("/user/hand/right")};
        XrActionSetCreateInfo setInfo{XR_TYPE_ACTION_SET_CREATE_INFO};
        std::strcpy(setInfo.actionSetName, "teleop");
        std::strcpy(setInfo.localizedActionSetName, "Teleoperation");
        XR(xrCreateActionSet(instance, &setInfo, &actionSet));
        poseAction = action("hand_pose", XR_ACTION_TYPE_POSE_INPUT);
        gripAction = action("grip", XR_ACTION_TYPE_FLOAT_INPUT);
        triggerAction = action("trigger", XR_ACTION_TYPE_FLOAT_INPUT);
        buttonAction = action("face_button", XR_ACTION_TYPE_BOOLEAN_INPUT);
        discardAction = action("discard_button", XR_ACTION_TYPE_BOOLEAN_INPUT);
        resetAction = action("reset_orientation", XR_ACTION_TYPE_BOOLEAN_INPUT);
        std::vector<XrActionSuggestedBinding> bindings;
        for (const char* hand : {"left", "right"}) {
            std::string base = std::string("/user/hand/") + hand + "/input/";
            bindings.push_back({poseAction, path((base + "grip/pose").c_str())});
            bindings.push_back({gripAction, path((base + "squeeze/value").c_str())});
            bindings.push_back({triggerAction, path((base + "trigger/value").c_str())});
            bindings.push_back({resetAction, path((base + "thumbstick/click").c_str())});
            bindings.push_back({buttonAction, path((base + (hand[0] == 'l' ? "x/click" : "a/click")).c_str())});
            bindings.push_back({discardAction, path((base + (hand[0] == 'l' ? "y/click" : "b/click")).c_str())});
        }
        XrInteractionProfileSuggestedBinding suggested{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
        suggested.interactionProfile = path("/interaction_profiles/oculus/touch_controller");
        suggested.countSuggestedBindings = bindings.size();
        suggested.suggestedBindings = bindings.data();
        XR(xrSuggestInteractionProfileBindings(instance, &suggested));
        XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO};
        attach.countActionSets = 1;
        attach.actionSets = &actionSet;
        XR(xrAttachSessionActionSets(session, &attach));
        for (int i = 0; i < 2; ++i) {
            XrActionSpaceCreateInfo spaceInfo{XR_TYPE_ACTION_SPACE_CREATE_INFO};
            spaceInfo.action = poseAction;
            spaceInfo.subactionPath = handPaths[i];
            spaceInfo.poseInActionSpace.orientation.w = 1;
            XR(xrCreateActionSpace(session, &spaceInfo, &handSpaces[i]));
        }
        proc("xrCreatePassthroughFB", createPassthrough);
        proc("xrDestroyPassthroughFB", destroyPassthrough);
        proc("xrPassthroughStartFB", startPassthrough);
        proc("xrPassthroughPauseFB", pausePassthrough);
        proc("xrCreatePassthroughLayerFB", createLayer);
        proc("xrDestroyPassthroughLayerFB", destroyLayer);
        proc("xrPassthroughLayerResumeFB", resumeLayer);
        proc("xrPassthroughLayerPauseFB", pauseLayer);
        XrPassthroughCreateInfoFB passInfo{XR_TYPE_PASSTHROUGH_CREATE_INFO_FB};
        XR(createPassthrough(session, &passInfo, &passthrough));
        XrPassthroughLayerCreateInfoFB layerInfo{XR_TYPE_PASSTHROUGH_LAYER_CREATE_INFO_FB};
        layerInfo.passthrough = passthrough;
        layerInfo.purpose = XR_PASSTHROUGH_LAYER_PURPOSE_RECONSTRUCTION_FB;
        XR(createLayer(session, &layerInfo, &passthroughLayer));
        LOG("Initialized OpenXR default passthrough; user presence=%d", presenceSupported);
    }
    void events() {
        XrEventDataBuffer event{XR_TYPE_EVENT_DATA_BUFFER};
        XrResult result;
        while ((result = xrPollEvent(instance, &event)) == XR_SUCCESS) {
            switch (event.type) {
            case XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED: {
                auto& e = reinterpret_cast<XrEventDataSessionStateChanged&>(event);
                focused = e.state == XR_SESSION_STATE_FOCUSED;
                LOG("session_state=%d", e.state);
                if (e.state == XR_SESSION_STATE_READY) {
                    XrSessionBeginInfo begin{XR_TYPE_SESSION_BEGIN_INFO};
                    begin.primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                    XR(xrBeginSession(session, &begin));
                    requestInput120();
                    XR(startPassthrough(passthrough));
                    XR(resumeLayer(passthroughLayer));
                    running = true;
                    LOG("PASSTHROUGH_ACTIVE");
                } else if (e.state == XR_SESSION_STATE_STOPPING) {
                    XR(pauseLayer(passthroughLayer));
                    XR(pausePassthrough(passthrough));
                    XR(xrEndSession(session));
                    running = false;
                } else if (e.state == XR_SESSION_STATE_EXITING || e.state == XR_SESSION_STATE_LOSS_PENDING) {
                    quit = true;
                }
                break;
            }
            case XR_TYPE_EVENT_DATA_USER_PRESENCE_CHANGED_EXT:
                present = reinterpret_cast<XrEventDataUserPresenceChangedEXT&>(event).isUserPresent;
                break;
            case XR_TYPE_EVENT_DATA_DISPLAY_REFRESH_RATE_CHANGED_FB:
                actualRefresh = reinterpret_cast<XrEventDataDisplayRefreshRateChangedFB&>(event).toDisplayRefreshRate;
                LOG("INPUT120 actual_hz=%.3f", actualRefresh);
                note(actualRefresh > 119 ? "DISPLAY 120 HZ" : "DISPLAY BELOW 120 HZ");
                break;
            case XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING: {
                auto& e = reinterpret_cast<XrEventDataReferenceSpaceChangePending&>(event);
                ++recenter;
                pendingRecenter = std::max(pendingRecenter, e.changeTime);
                note("RECENTER: CALIBRATE ON PC");
                LOG("reference space changed; recalibration required");
                break;
            }
            case XR_TYPE_EVENT_DATA_PASSTHROUGH_STATE_CHANGED_FB: {
                auto flags = reinterpret_cast<XrEventDataPassthroughStateChangedFB&>(event).flags;
                if (flags & (XR_PASSTHROUGH_STATE_CHANGED_REINIT_REQUIRED_BIT_FB |
                             XR_PASSTHROUGH_STATE_CHANGED_NON_RECOVERABLE_ERROR_BIT_FB)) {
                    throw std::runtime_error("Passthrough lost; restart app");
                }
                if (flags & XR_PASSTHROUGH_STATE_CHANGED_RECOVERABLE_ERROR_BIT_FB) passthroughHealthy = false;
                if (flags & XR_PASSTHROUGH_STATE_CHANGED_RESTORED_ERROR_BIT_FB) passthroughHealthy = true;
                LOG("passthrough_state=%llu", (unsigned long long)flags);
                break;
            }
            case XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING: quit = true; break;
            case XR_TYPE_EVENT_DATA_EVENTS_LOST:
                throw std::runtime_error("XR events lost; restart to establish coordinate origin");
            default: break;
            }
            event = {XR_TYPE_EVENT_DATA_BUFFER};
        }
        if (result != XR_EVENT_UNAVAILABLE) XR(result);
    }
    void frame() {
        XrFrameWaitInfo wait{XR_TYPE_FRAME_WAIT_INFO};
        XrFrameState state{XR_TYPE_FRAME_STATE};
        XR(xrWaitFrame(session, &wait, &state));
        XrFrameBeginInfo begin{XR_TYPE_FRAME_BEGIN_INFO};
        XR(xrBeginFrame(session, &begin));
        XrActiveActionSet active{actionSet, XR_NULL_PATH};
        XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO};
        sync.countActiveActionSets = 1;
        sync.activeActionSets = &active;
        XrResult syncResult = xrSyncActions(session, &sync);
        XR(syncResult);
        bool usable = focused && state.shouldRender && passthroughHealthy &&
                      state.predictedDisplayTime >= pendingRecenter && syncResult == XR_SUCCESS;
        XrSpaceLocation head{XR_TYPE_SPACE_LOCATION};
        XrResult headResult = xrLocateSpace(view, floor, state.predictedDisplayTime, &head);
        headTracked = XR_SUCCEEDED(headResult) && (head.locationFlags & 15) == 15;
        updateInputRate();
        std::ostringstream out;
        out << std::setprecision(12) << std::boolalpha;
        out << "{\"protocol\":3,\"session\":\"xr-" << stream << "\",\"seq\":" << ++sequence
            << ",\"sample_ns\":" << bootNs() << ",\"predicted_s\":" << state.predictedDisplayTime / 1e9
            << ",\"space\":1,\"recenter\":" << recenter << ",\"focused\":" << usable
            << ",\"mounted\":" << (presenceSupported ? present : focused)
            << ",\"head_status\":" << (XR_SUCCEEDED(headResult) ? head.locationFlags & 15 : 0)
            << ",\"head\":";
        poseJson(out, head.pose);
        out << ",\"refresh\":{\"requested_hz\":120,\"actual_hz\":" << actualRefresh
            << ",\"input_hz\":" << inputHz << ",\"supported\":" << refreshSupported
            << ",\"request_result\":" << refreshResult
            << ",\"predicted_period_ns\":" << state.predictedDisplayPeriod << "},\"hands\":{";
        for (int i = 0; i < 2; ++i) {
            XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO};
            get.subactionPath = handPaths[i];
            XrActionStatePose pose{XR_TYPE_ACTION_STATE_POSE};
            get.action = poseAction;
            XR(xrGetActionStatePose(session, &get, &pose));
            XrActionStateFloat grip{XR_TYPE_ACTION_STATE_FLOAT}, trigger{XR_TYPE_ACTION_STATE_FLOAT};
            get.action = gripAction;
            XR(xrGetActionStateFloat(session, &get, &grip));
            get.action = triggerAction;
            XR(xrGetActionStateFloat(session, &get, &trigger));
            XrActionStateBoolean button{XR_TYPE_ACTION_STATE_BOOLEAN};
            get.action = buttonAction;
            XR(xrGetActionStateBoolean(session, &get, &button));
            XrActionStateBoolean discard{XR_TYPE_ACTION_STATE_BOOLEAN};
            get.action = discardAction;
            XR(xrGetActionStateBoolean(session, &get, &discard));
            XrActionStateBoolean reset{XR_TYPE_ACTION_STATE_BOOLEAN};
            get.action = resetAction;
            XR(xrGetActionStateBoolean(session, &get, &reset));
            XrSpaceLocation loc{XR_TYPE_SPACE_LOCATION};
            XrResult located = xrLocateSpace(handSpaces[i], floor, state.predictedDisplayTime, &loc);
            handActive[i] = pose.isActive && grip.isActive && trigger.isActive && button.isActive && discard.isActive && reset.isActive;
            handTracked[i] = handActive[i] && XR_SUCCEEDED(located) && (loc.locationFlags & 15) == 15;
            if (i == 1) { rightGrip = grip.currentState; rightTrigger = trigger.currentState; }
            if (i) out << ',';
            out << '"' << (i ? 'r' : 'l') << "\":{\"device\":" << i + 1
                << ",\"tracking_ok\":" << bool(pose.isActive && XR_SUCCEEDED(located))
                << ",\"input_ok\":" << bool(usable && grip.isActive && trigger.isActive && button.isActive && discard.isActive && reset.isActive)
                << ",\"status\":" << (XR_SUCCEEDED(located) ? loc.locationFlags & 15 : 0)
                << ",\"pose_s\":" << state.predictedDisplayTime / 1e9
                << ",\"input_s\":" << bootNs() / 1e9 << ",\"recenter\":" << recenter
                << ",\"buttons\":" << inputButtons(button.currentState, discard.currentState, reset.currentState)
                << ",\"grip\":" << grip.currentState << ",\"trigger\":" << trigger.currentState
                << ",\"pose\":";
            poseJson(out, loc.pose);
            out << '}';
        }
        out << "}}";
        writeInputPacket(out.str());
        if (state.shouldRender) updateHud(usable && (presenceSupported ? present : focused));
        XrCompositionLayerPassthroughFB layer{XR_TYPE_COMPOSITION_LAYER_PASSTHROUGH_FB};
        layer.layerHandle = passthroughLayer;
        layer.space = XR_NULL_HANDLE;
        XrCompositionLayerQuad hud{XR_TYPE_COMPOSITION_LAYER_QUAD};
        hud.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
        hud.space = view;
        hud.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
        hud.subImage.swapchain = hudSwapchain;
        hud.subImage.imageRect.extent = {StatusPanel::width, StatusPanel::height};
        hud.pose.orientation.w = 1;
        hud.pose.position = {.43f, .30f, -1.20f};
        hud.size = {.56f, .315f};
        const XrCompositionLayerBaseHeader* layers[] = {
            reinterpret_cast<XrCompositionLayerBaseHeader*>(&layer),
            reinterpret_cast<XrCompositionLayerBaseHeader*>(&hud)};
        XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO};
        end.displayTime = state.predictedDisplayTime;
        end.environmentBlendMode = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
        end.layerCount = state.shouldRender && passthroughHealthy ? (hudReady ? 2 : 1) : 0;
        end.layers = layers;
        XR(xrEndFrame(session, &end));
    }
public:
    explicit InputApp(android_app* a) : app(a) {}
    void run() {
        init();
        while (!app->destroyRequested && !quit) {
            android_poll_source* source;
            int eventsCount;
            int ident;
            while ((ident = ALooper_pollAll(running ? 0 : 100, nullptr, &eventsCount,
                                            reinterpret_cast<void**>(&source))) >= 0) {
                if (source) source->process(app, source);
                if (app->destroyRequested) return;
                if (!running) break;
            }
            events();
            if (running && !quit) frame();
        }
    }
    ~InputApp() {
        if (hudSwapchain) xrDestroySwapchain(hudSwapchain);
        if (passthroughLayer) destroyLayer(passthroughLayer);
        if (passthrough) destroyPassthrough(passthrough);
        for (auto s : handSpaces) if (s) xrDestroySpace(s);
        if (view) xrDestroySpace(view);
        if (floor) xrDestroySpace(floor);
        if (session) xrDestroySession(session);
        if (actionSet) xrDestroyActionSet(actionSet);
        if (instance) xrDestroyInstance(instance);
        if (display != EGL_NO_DISPLAY) {
            eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
            if (surface != EGL_NO_SURFACE) eglDestroySurface(display, surface);
            if (context != EGL_NO_CONTEXT) eglDestroyContext(display, context);
            eglTerminate(display);
        }
        app->activity->vm->DetachCurrentThread();
    }
};
void android_main(android_app* app) {
    try {
        InputApp input(app);
        input.run();
    } catch (const std::exception& error) {
        __android_log_print(ANDROID_LOG_ERROR, "SawyerPassthrough", "FAILED: %s", error.what());
    }
    ANativeActivity_finish(app->activity);
}
