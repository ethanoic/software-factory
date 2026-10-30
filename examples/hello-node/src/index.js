#!/usr/bin/env node
"use strict";

const { greet } = require("./greet");

// Usage: node src/index.js [name]
console.log(greet(process.argv[2]));
