// Compiles Helper.sol with solc-js and writes its runtime bytecode to src/helper.hex.
// Run: (cd helper && npm install && node compile.cjs)
const fs = require('fs');
const path = require('path');
const solc = require('solc');

const source = fs.readFileSync(path.join(__dirname, 'Helper.sol'), 'utf8');
const input = {
  language: 'Solidity',
  sources: { 'Helper.sol': { content: source } },
  settings: {
    optimizer: { enabled: true, runs: 1000 },
    viaIR: true,
    evmVersion: 'cancun',
    outputSelection: { '*': { Helper: ['evm.deployedBytecode.object', 'abi'] } },
  },
};
const out = JSON.parse(solc.compile(JSON.stringify(input)));
const errors = (out.errors || []).filter((e) => e.severity === 'error');
for (const e of out.errors || []) console.error(e.formattedMessage);
if (errors.length) process.exit(1);
const runtime = out.contracts['Helper.sol'].Helper.evm.deployedBytecode.object;
fs.writeFileSync(path.join(__dirname, '..', 'src', 'helper.hex'), runtime);
console.log(`helper runtime: ${runtime.length / 2} bytes`);
