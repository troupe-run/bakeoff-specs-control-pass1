"""Errors carrying the CLI exit code defined by the contract."""


class TroupeError(Exception):
    code = 1

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UsageError(TroupeError):
    code = 2


class WriteRefused(TroupeError):
    code = 3


class LoadError(TroupeError):
    code = 4


class PathError(TroupeError):
    code = 5
