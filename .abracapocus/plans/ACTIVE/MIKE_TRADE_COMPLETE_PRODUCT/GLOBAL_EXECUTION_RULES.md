# Global Execution Rules

- The explicit structured task contract is authoritative.
- Respect each task's allowed_write_paths exactly as authored.
- Do not infer or add files, tests, verification commands, acceptance criteria, dependencies, prerequisites, backend assignments, or lifecycle outcomes from prose, keywords, filenames, naming conventions, or repository shape.
- Verification commands run only when they are explicitly present in the materialized task or phase contract. Verification observations do not override backend execution facts.
- Do not modify state/runtime_state.json, reports/, logs/, or virtual environments unless an explicit task contract permits it.
