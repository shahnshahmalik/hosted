# Third-party notices

Hosted itself is MIT licensed (see [LICENSE](LICENSE)). It bundles the following third-party
components, each under its own license:

| Component | Where | License |
|---|---|---|
| [Preact](https://preactjs.com) (`preact`, `preact/hooks`) | `static/vendor/` | MIT, [text](static/vendor/licenses/preact-MIT.txt) |
| [htm](https://github.com/developit/htm) | `static/vendor/` | Apache-2.0, [text](static/vendor/licenses/htm-Apache-2.0.txt) |
| [Space Grotesk](https://github.com/floriankarsten/space-grotesk) | `static/fonts/` | SIL OFL 1.1, [text](static/fonts/licenses/SpaceGrotesk-OFL.txt) |
| [Chakra Petch](https://github.com/m4rc1e/Chakra-Petch) | `static/fonts/` | SIL OFL 1.1, [text](static/fonts/licenses/ChakraPetch-OFL.txt) |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | `static/fonts/` | SIL OFL 1.1, [text](static/fonts/licenses/JetBrainsMono-OFL.txt) |

Python dependencies (FastAPI, Uvicorn, HTTPX, Anthropic SDK, psutil) are installed from PyPI at
build time and are not redistributed in this repository.

Services used at runtime (not bundled): service icons are loaded by the Services widget from the
[dashboard-icons](https://github.com/homarr-labs/dashboard-icons) project via jsDelivr; data comes
from Open-Meteo, CoinGecko, Frankfurter (ECB rates), the Hacker News API and the GitHub API.
