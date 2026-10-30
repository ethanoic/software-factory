"use strict";

const { greet } = require("../src/greet");

describe("greet", () => {
  test("greets a person by name", () => {
    expect(greet("Ada")).toBe("Hello, Ada!");
  });

  test("greets the world when no name is given", () => {
    expect(greet()).toBe("Hello, World!");
    expect(greet("")).toBe("Hello, World!");
  });
});
