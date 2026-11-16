# Contributing

## Report a bug

Open an issue with the Quest Guard version, WoW interface number, RestedXP version, steps to reproduce, and expected and actual behavior. Use a minimal fictional route or snapshot when guide or character data is needed. Share security issues through the [private reporting process](SECURITY.md).

Check text and attachments for API keys, account and character identifiers, realm names, private paths, and copyrighted content. Leave local logs, SavedVariables, generated addon data, quest caches and third-party guide content out of issues and pull requests.

## Make a change

1. Start from the default branch of a fork. Keep each change focused and note any compatibility effect.
2. Run the automated checks and add a synthetic regression fixture if a case needs coverage.
3. Open a pull request describing the change, tests run, and any live verification still needed. By submitting, you agree that your original contribution can be distributed under the repository's [MIT license](LICENSE). Identify third-party dependencies and their notices separately.

```powershell
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python scripts/build_release.py --interface 16001 --addon-only
```

Use a 64-bit Windows Python installation with Tk for the desktop UI checks. The test suite uses Lua 5.1 through Lupa and mocked provider responses. See [build instructions](README.md#build-and-verify) for the Windows package and [verification notes](docs/VERIFICATION.md) for fixture coverage.
