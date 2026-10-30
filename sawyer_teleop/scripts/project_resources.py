"""Fixed sibling resources for development; bundled resources for deployments."""
from pathlib import Path


def windows_runtime(root):
    root = Path(root)
    # An incomplete bundled runtime must fail locally, not borrow another copy.
    return root / 'runtime' if (root / 'runtime').exists() else root.parent / 'windows-runtime'


def gazebo_cache(root):
    root = Path(root)
    return root / 'runtime/docker' if (root / 'runtime').exists() else root.parent / 'gazebo-image'
