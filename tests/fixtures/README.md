# Test fixtures

Drop real sample PDFs here to include them in `test_samples.py` and
`test_content.py`. This folder is **gitignored for PDFs on purpose**: the
samples are real supplier statements containing counterparty names, balances
and contact details — financial data that should not live in a git history.

Without fixtures, those two suites skip their real-document cases and still
run their synthetic ones; the other four suites are fully synthetic and never
need fixtures.

Expected files for the full regression:
  SYSTEM_SIDE_-_One_world_SOA_11_07_25.pdf
  SYSTEM_SIDE_-_Nutripharm_SOA_19_04_24.pdf
