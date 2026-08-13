#!/usr/bin/env node
// settings-test-inventory.mjs — semantic inventory of the SettingsOverlay test
// estate (#513 S2, Task 1).
//
// The consolidation in Task 2 moves ~94 cases out of six files into three. The
// only guarantee against silent coverage loss is a per-case record that is
// stronger than "an assertion with this text still exists somewhere": identical
// assertion text does not establish identical setup or identical stimulus. The
// tableSort suite alone carries three cases whose assertions read almost the
// same while their setups differ (zero-override disablement, deferred staging,
// Save-time clearing).
//
// So each case is recorded as {file, qualifiedName, paramExpansionCount,
// callbackHash, hooks}. `callbackHash` hashes the test body's source text with
// insignificant whitespace collapsed, so a verbatim move is recognisable by
// hash alone; a case whose hash changed must carry an explicit written
// equivalence record naming its survivor.
//
// Parsing goes through the TypeScript compiler already in node_modules. TSX is
// not regex-parseable, and a regex inventory would silently miss exactly the
// nested `describe` blocks that make qualified names unique.

import ts from 'typescript';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const HOOK_NAMES = new Set(['beforeEach', 'afterEach', 'beforeAll', 'afterAll']);
const TEST_NAMES = new Set(['it', 'test']);

function hashText(text) {
  // Collapse runs of whitespace so a reindent (moving a case into a differently
  // nested describe) is not reported as a changed body. Anything else — an
  // added assertion, a changed selector, a different stimulus — still moves the
  // hash.
  const normalized = text.replace(/\s+/g, ' ').trim();
  return createHash('sha256').update(normalized).digest('hex').slice(0, 16);
}

function literalText(node) {
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  return null;
}

// Render one `it.each` row into the concrete title vitest would print.
function formatEachTitle(template, row) {
  let index = 0;
  return template.replace(/%[sdifjop#%]/g, (token) => {
    if (token === '%%') return '%';
    const value = row[index++];
    return value === undefined ? token : value;
  });
}

// The argument rows of `it.each([...])`. Each row is either an array literal
// (multiple parameters) or a single expression (one parameter).
function eachRows(arrayLiteral) {
  return arrayLiteral.elements.map((element) => {
    if (ts.isArrayLiteralExpression(element)) {
      return element.elements.map((cell) => {
        const asLiteral = literalText(cell);
        if (asLiteral !== null) return asLiteral;
        // `'daily' as const` and friends — unwrap the assertion.
        if (ts.isAsExpression(cell) || ts.isTypeAssertionExpression(cell)) {
          const inner = literalText(cell.expression);
          if (inner !== null) return inner;
        }
        return cell.getText();
      });
    }
    const asLiteral = literalText(element);
    return [asLiteral === null ? element.getText() : asLiteral];
  });
}

// `it`, `it.only`, `it.skip`, `it.each([...])`, `it.concurrent` … — return the
// base identifier ('it' / 'test' / 'describe') plus the modifier chain.
function callRoot(expression) {
  const modifiers = [];
  let current = expression;
  while (ts.isPropertyAccessExpression(current)) {
    modifiers.unshift(current.name.text);
    current = current.expression;
  }
  if (ts.isCallExpression(current)) {
    // `it.each([...])(title, cb)` — the callee is itself a call.
    const inner = callRoot(current.expression);
    return inner && { ...inner, eachCall: current, modifiers: [...inner.modifiers, ...modifiers] };
  }
  if (!ts.isIdentifier(current)) return null;
  return { name: current.text, modifiers, eachCall: null };
}

function collectHooks(statements) {
  const hooks = [];
  for (const statement of statements) {
    if (!ts.isExpressionStatement(statement)) continue;
    const call = statement.expression;
    if (!ts.isCallExpression(call)) continue;
    const root = callRoot(call.expression);
    if (!root || !HOOK_NAMES.has(root.name)) continue;
    const body = call.arguments[0];
    hooks.push({ kind: root.name, hash: body ? hashText(body.getText()) : 'none' });
  }
  return hooks;
}

function inventoryFile(fileName, repoRelative) {
  const source = ts.createSourceFile(
    fileName,
    readFileSync(fileName, 'utf8'),
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    ts.ScriptKind.TSX,
  );
  const cases = [];

  function walkScope(statements, titles, inheritedHooks) {
    const hooks = [...inheritedHooks, ...collectHooks(statements)];
    for (const statement of statements) {
      if (!ts.isExpressionStatement(statement)) continue;
      const call = statement.expression;
      if (!ts.isCallExpression(call)) continue;
      const root = callRoot(call.expression);
      if (!root) continue;

      if (root.name === 'describe') {
        const title = literalText(call.arguments[0]);
        const body = call.arguments[1];
        if (title === null || !body || !ts.isFunctionLike(body) || !body.body) continue;
        if (!ts.isBlock(body.body)) continue;
        walkScope(body.body.statements, [...titles, title], hooks);
        continue;
      }

      if (!TEST_NAMES.has(root.name)) continue;
      const title = literalText(call.arguments[0]);
      const callback = call.arguments[1];
      if (title === null || !callback) continue;
      const callbackHash = hashText(callback.getText());

      if (root.eachCall) {
        const table = root.eachCall.arguments[0];
        if (!table || !ts.isArrayLiteralExpression(table)) continue;
        const rows = eachRows(table);
        for (const row of rows) {
          cases.push({
            file: repoRelative,
            qualifiedName: [...titles, formatEachTitle(title, row)].join(' > '),
            paramExpansionCount: rows.length,
            callbackHash,
            hooks,
          });
        }
        continue;
      }

      cases.push({
        file: repoRelative,
        qualifiedName: [...titles, title].join(' > '),
        paramExpansionCount: 1,
        callbackHash,
        hooks,
      });
    }
  }

  walkScope(source.statements, [], []);
  return cases;
}

// The `--out` destination and the positional targets. Split out and exported
// so the one arithmetic it does is testable: `outIndex + 1` is `0` when `--out`
// is ABSENT, so filtering positionals on `i !== outIndex + 1` dropped `argv[0]`
// — the first file named on the command line — from every stdout-mode run. A
// tool whose whole purpose is preventing silent coverage loss must not lose a
// file in silence, which is what that did: 83 cases across two files reported
// where three files hold 165.
export function parseInventoryArgs(argv) {
  const outIndex = argv.indexOf('--out');
  const outValueIndex = outIndex === -1 ? -1 : outIndex + 1;
  return {
    outPath: outIndex === -1 ? null : (argv[outValueIndex] ?? null),
    explicit: argv.filter((arg, i) => !arg.startsWith('--') && i !== outValueIndex),
  };
}

function main() {
  const argv = process.argv.slice(2);
  const { outPath, explicit } = parseInventoryArgs(argv);

  const webDir = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
  const targets = explicit.length
    ? explicit
    : [
        'src/components/SettingsOverlay.test.tsx',
        'src/components/SettingsOverlay.lanAuth.test.tsx',
        'src/components/SettingsOverlay.source.test.tsx',
        '__tests__/SettingsOverlay.test.tsx',
        '__tests__/SettingsOverlay-layout.test.tsx',
        '__tests__/SettingsOverlay-tableSort.test.tsx',
      ];

  const cases = [];
  const perFile = {};
  for (const target of targets) {
    const absolute = path.resolve(webDir, target);
    const found = inventoryFile(absolute, target);
    perFile[target] = found.length;
    cases.push(...found);
  }

  const payload = { total: cases.length, perFile, cases };
  const rendered = `${JSON.stringify(payload, null, 2)}\n`;
  if (outPath) {
    writeFileSync(path.resolve(process.cwd(), outPath), rendered);
    for (const [file, count] of Object.entries(perFile)) {
      process.stderr.write(`${String(count).padStart(4)}  ${file}\n`);
    }
    process.stderr.write(`${String(cases.length).padStart(4)}  TOTAL\n`);
  } else {
    process.stdout.write(rendered);
  }
}

// Only when run as a program. Importing the module to test `parseInventoryArgs`
// must not run an inventory over the importer's own argv.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
