# factory-hello-node

A deliberately tiny Node.js app for trying the AI Software Factory end to end. It has one function,
`greet(name)`, Jest tests in `tests/`, and an ESLint config the factory's runner can load as is.

```bash
npm install
npm test          # 2 passing tests
npm run lint
npm start         # Hello, World!
npm start -- Ada  # Hello, Ada!
```

To have the factory work on this repo, follow the walkthrough in the factory repository:
[`docs/hello-world-walkthrough.md`](https://github.com/ethanoic/software-factory/blob/main/docs/hello-world-walkthrough.md).

Rules the factory enforces on this repo:

- `tests/` is frozen for each run: the factory writes new tests during planning and cannot edit them afterwards.
- `package.json`, `package-lock.json` and `.github/` are never changed by the factory. Add dependencies yourself,
  then rebuild the runner image (`make runner-ts DEPS=<path to this repo>` in the factory folder).
