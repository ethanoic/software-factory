"use strict";

// ESLint 9 flat config with no plugins, so the factory's runner can load it with nothing extra installed.
// The factory lints only the files a patch changes; errors here send the patch back for repair.

const node = {
  require: "readonly", module: "writable", exports: "writable", process: "readonly",
  console: "readonly", __dirname: "readonly", __filename: "readonly",
  setTimeout: "readonly", clearTimeout: "readonly", Buffer: "readonly",
};
const jest = {
  describe: "readonly", test: "readonly", it: "readonly", expect: "readonly",
  beforeEach: "readonly", afterEach: "readonly", beforeAll: "readonly", afterAll: "readonly", jest: "readonly",
};

module.exports = [
  { ignores: ["node_modules/**", ".factory/**", "coverage/**"] },
  {
    files: ["**/*.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "commonjs", globals: node },
    rules: {
      "no-undef": "error",
      "no-unused-vars": "error",
      "no-unreachable": "error",
      "no-dupe-keys": "error",
      eqeqeq: ["error", "smart"],
    },
  },
  { files: ["tests/**/*.js"], languageOptions: { globals: { ...node, ...jest } } },
];
