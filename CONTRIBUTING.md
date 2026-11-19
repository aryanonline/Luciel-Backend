# Contributing

Thanks for your interest in Luciel.

## Questions, bugs and ideas

Open an issue on GitHub. For a security problem, follow [`SECURITY.md`](SECURITY.md) instead of
opening a public issue.

## Pull requests

Luciel is maintained by one developer and does not take outside pull requests at the moment. You
are welcome to fork it under the [AGPL-3.0](LICENSE) and to suggest a change in an issue.

## Working on the code

- Run the whole product with Docker: see
  [Run Luciel on your computer](README.md#run-luciel-on-your-computer).
- Run the backend from a virtual environment and the test suite: see
  [Local development](README.md#local-development) and
  [Tests and quality checks](README.md#tests-and-quality-checks).
- Before a change is done, `ruff check app tests scripts`, `ruff format --check app tests scripts`,
  `mypy app` and the full test suite pass.
- The `CLAUDE.md` files are the working guides to each part of the codebase: the product rules
  the code keeps, where things live and the tests that pin them.
