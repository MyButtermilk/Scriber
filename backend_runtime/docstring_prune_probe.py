"""Frozen-only sentinel for assertion-preserving recursive docstring deletion."""


class DocstringPruneProbe:
    """Class-level sentinel."""

    def method(self) -> bool:
        """Method-level sentinel."""

        return True


def assertions_enabled() -> bool:
    """Return true only when Python assertions remain executable."""

    try:
        assert False, "frozen assertion sentinel"
    except AssertionError:
        return True
    return False
