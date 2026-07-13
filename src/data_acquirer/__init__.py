"""DataAcquirer — a visual InfluxDB (v1 / InfluxQL) time-series puller.

Refactored from the original ``loadDataV1`` script into a configurable
GUI application. The acquisition logic lives in :mod:`data_acquirer.core`,
the desktop UI in :mod:`data_acquirer.ui`, and the headless command-line
mode (``pull`` / ``test`` / ``points``) in :mod:`data_acquirer.cli`.
"""

__version__ = "1.1.0"
