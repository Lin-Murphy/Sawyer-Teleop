"""Install the single verified 120 Hz APK from this source checkout."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from sawyer_teleop.quest_install import install_verified_quest


def install(serial=None, adb=None):
    return install_verified_quest(ROOT, serial=serial, adb=adb)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--adb')
    args = parser.parse_args()
    install(args.serial, args.adb)
