const fs = require("fs");
const vm = require("vm");

// read local_pool.js
const code = fs.readFileSync("local_pool.js", "utf8");

// create sandbox
const sandbox = {};
vm.createContext(sandbox);

// run the JS file
vm.runInContext(code, sandbox);

// LOCAL_POOL is created automatically
const pool = sandbox.LOCAL_POOL;

if (!pool || !Array.isArray(pool)) {
  console.error("LOCAL_POOL not found.");
  process.exit(1);
}

// save JSON
fs.writeFileSync(
  "questions.json",
  JSON.stringify(pool, null, 2)
);

console.log("questions.json created with", pool.length, "questions");