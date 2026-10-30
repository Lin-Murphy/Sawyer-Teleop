#pragma once
#include <cmath>
#include <vector>

// The product default is 120 Hz. Never silently select 72/80/90 instead.
inline bool supportsInput120(const std::vector<float>& rates) {
    for (float rate : rates)
        if (std::isfinite(rate) && std::fabs(rate - 120.0f) < .01f) return true;
    return false;
}

inline unsigned inputButtons(bool primary, bool secondary, bool stick) {
    return (primary ? 1u : 0u) | (secondary ? 2u : 0u) | (stick ? 4u : 0u);
}
