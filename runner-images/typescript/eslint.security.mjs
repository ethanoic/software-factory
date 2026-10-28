// Security-only lint used by the QA stage (spec §1.3). Findings at "error" severity block the PR.
import security from "eslint-plugin-security";

export default [
  { ignores: ["node_modules/**", ".factory/**", "tests/**", "dist/**"] },
  security.configs.recommended,
];
