"""Reproduce the approved binary subset from the explicit local theme checkout."""

import hashlib
from pathlib import Path

SOURCE = Path("/home/ai-agent/k8s_stack/keycloak_theme")
REVISION = "32f99f2833ee4e2acb528bbe2c0702900d24beed"
ASSETS = {
    "fonts/OFL.txt": "262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a",
    "fonts/Inter-Regular.ttf": "1b08e7fc267a5c7e1d614100f604b83e7e8a0be241f0f288faa2b3ac93a683ba",
    "fonts/Inter-SemiBold.ttf": "e7a1aaf7eda9f2fad4131725fa556265ec75ca7b2d756260173a040363e8d4f7",
    "img/logo_black.png": "ee763463266782b3e678e07b2f80228db974c393494071f0de227dd0ce7c7fc8",
}


def main() -> None:
    destination = Path(__file__).parent / "assets"
    for relative, digest in ASSETS.items():
        data = (SOURCE / "theme/neurwerk/login/resources" / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Theme asset checksum mismatch: {relative}")
        (destination / Path(relative).name).write_bytes(data)


if __name__ == "__main__":
    main()
