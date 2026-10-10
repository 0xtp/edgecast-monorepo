# Test fixtures

The build environment could not reach Polymarket, Open-Meteo, IEM or NWS, so **no fixture here was recorded
from a live API**. `weather.py` builds synthetic payloads in the documented response shapes (Gamma events with
nested markets, CLOB `/book` and `/clob-markets`, Open-Meteo ensemble `hourly` members, IEM CSV).

Once the hosts are reachable, run `python scripts/inspect_markets.py --out tests/fixtures/gamma` to capture
real Gamma payloads; `tests/test_parser_recorded.py` then parses every captured event automatically.
