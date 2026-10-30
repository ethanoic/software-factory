// Security-only lint used by the QA stage (spec §1.3). Severity "error" = high finding: the PR is blocked
// and the patch goes to a security_fix repair. "warn" = medium: reported, not blocking.
// The plugin's recommended set is all "warn", which would never block anything, so the clearly
// dangerous patterns are raised to "error" here. The noisy heuristics stay at "warn".
import security from "eslint-plugin-security";

const BLOCKING = [
  "detect-eval-with-expression",
  "detect-child-process",
  "detect-non-literal-require",
  "detect-unsafe-regex",
  "detect-buffer-noassert",
  "detect-new-buffer",
  "detect-pseudoRandomBytes",
  "detect-disable-mustache-escape",
  "detect-no-csrf-before-method-override",
  "detect-bidi-characters",
];

export default [
  { ignores: ["node_modules/**", ".factory/**", "tests/**", "dist/**", "coverage/**"] },
  security.configs.recommended,
  {
    rules: {
      ...Object.fromEntries(BLOCKING.map((r) => [`security/${r}`, "error"])),
      "no-eval": "error",
      "no-new-func": "error",
      "no-implied-eval": "error",
    },
  },
];
