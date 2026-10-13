### FFL Core

FFL gun store core: Item/Customer extensions, A&D doctypes, Intake Tool

### Installation

This app is one of three in the GunStore POS monorepo and is not installed on its own. See the [root README](../../README.md) for the dev stack and the [self-hosting guide](../../docs/self-hosting.md) for a server.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ffl_core
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

GNU General Public License v3.0 (GPL-3.0); see `license.txt`.
