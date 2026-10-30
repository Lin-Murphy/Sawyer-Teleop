"""Motion choices and their backend capability requirements."""

from .intera_cartesian import SEND_HZ_CHOICES

# Keep the original saved key for the 10 Hz comparison option.
CARTESIAN_RATES = {('intera_cartesian' if hz == 10 else 'intera_cartesian_'+str(hz)+'hz'): hz
                   for hz in SEND_HZ_CHOICES}

DIRECT_POSE_MODE = 'intera_pose_50hz'
CARTESIAN_RATES[DIRECT_POSE_MODE] = 50

MOTION_PROFILES = {
    "standard": ("Standard", "Original acceleration limits; comparison baseline."),
    "fast_follow": ("Fast follow (experimental)",
                    "Real Sawyer: 2x Standard acceleration, same speed limits. Gazebo uses configured limits. Field validation pending."),
    **{key: ("Intera Cartesian ("+str(hz)+" Hz)",
             "Experimental. Target "+str(hz)+" native goals/s; actual timing depends on input, FK and the B control loop. Field validation pending.")
       for key, hz in CARTESIAN_RATES.items()},
}


MOTION_PROFILES[DIRECT_POSE_MODE] = ('Direct pose (50 Hz, experimental)', 'No application IK/FK; fixed engagement joint bias. Requires matching B.')


def selected_motion_profile(value, acceleration_override):
    # Preserve old configurations, which selected the override after a Gazebo handshake.
    if value is None:
        return "fast_follow" if acceleration_override is not None else "standard"
    if value not in MOTION_PROFILES:
        raise ValueError("Unknown motion profile: " + str(value))
    return value


def motion_profile_reason(profile, backend, capability=None):
    if profile == "fast_follow" and backend not in ("gazebo", "intera"):
        return "Fast follow requires Gazebo or Real Sawyer."
    if profile in CARTESIAN_RATES:
        if backend not in ("intera", "gazebo"):
            return "Intera Cartesian requires Real Sawyer or Gazebo with the simulated Motion Controller."
        capability = capability or {}
        if capability.get("available") is not True or capability.get("schema") != 1:
            return capability.get("reason") or "Connect to an updated B to check native Motion Controller availability."
        if profile == DIRECT_POSE_MODE and capability.get('direct_pose_50hz') is not True:
            return 'Update B: Direct pose 50 Hz requires the matching new B.'
        if CARTESIAN_RATES[profile] not in capability.get("send_hz_choices", []):
            return "Update B and reconnect to select Intera Cartesian submission rates."
    return None
