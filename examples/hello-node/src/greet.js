"use strict";

/**
 * Returns a greeting for `name`, or for the world when no name is given.
 * @param {string} [name]
 * @returns {string}
 */
function greet(name) {
  if (name === undefined || name === null || name === "") {
    return "Hello, World!";
  }
  return `Hello, ${name}!`;
}

module.exports = { greet };
