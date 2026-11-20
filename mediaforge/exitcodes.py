"""Process exit codes.

Shared by the CLI and the daemon, which shells out to it: the daemon has to
recognise ``EXIT_LOCKED`` without importing the command line, and a duplicated
literal in two modules is how the two drift apart.
"""

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2
EXIT_UNREACHABLE = 3
EXIT_LOCKED = 4

__all__ = [
    "EXIT_OK",
    "EXIT_PROBLEMS",
    "EXIT_CONFIG",
    "EXIT_UNREACHABLE",
    "EXIT_LOCKED",
]
