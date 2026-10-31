"""Stable release validation and ordering, shared with the standalone test launcher."""
import re


class UpdateError(Exception):
    pass


def version_tuple(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', version):
        raise UpdateError('发行版本号无效，需要正式版本 vX.Y.Z')
    return tuple(int(value) for value in version.lstrip('v').split('.'))


def version_order(version: str) -> tuple[int, int, int, int]:
    """Keep the two historical 2.4.4 fixes before the subsequent 2.4.5."""
    numbers = version_tuple(version)
    if numbers in ((2, 4, 41), (2, 4, 42)):
        return 2, 4, 4, numbers[2] - 40
    return *numbers, 0
