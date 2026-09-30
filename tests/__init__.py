
# Whether this machine has built the level tap plugin must not change what a
# channel renders in a test. Tests that want taps patch this back to True.
from unittest import mock as _mock

_mock.patch("audiorouter.channels.taps_available", return_value=False).start()
