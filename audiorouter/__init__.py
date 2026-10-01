"""AudioRouter: per-application audio routing with independent per-channel effects.

A "channel" is a named PipeWire sink backed by its own filter-chain process,
bound to one real output device. Applications are routed to channels, so two
apps playing at once can receive genuinely different effect processing.
"""

__version__ = "0.9.7"
