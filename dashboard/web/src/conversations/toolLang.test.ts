import { describe, expect, it } from 'vitest';
import { fileLangForCall, langFromExtension, resultLang } from './toolLang';

describe('langFromExtension', () => {
  it('maps known extensions to refractor languages', () => {
    expect(langFromExtension('/a/b/foo.py')).toBe('python');
    expect(langFromExtension('/a/b/foo.tsx')).toBe('tsx');
    expect(langFromExtension('/a/b/foo.ts')).toBe('typescript');
    expect(langFromExtension('/a/b/foo.json')).toBe('json');
    expect(langFromExtension('/a/b/foo.sh')).toBe('bash');
    expect(langFromExtension('/a/b/foo.yaml')).toBe('yaml');
    expect(langFromExtension('/a/b/foo.md')).toBe('markdown');
  });
  it('returns "" for unknown ext, dotfiles, and no extension', () => {
    expect(langFromExtension('/a/b/foo.html')).toBe('');
    expect(langFromExtension('/a/b/Makefile')).toBe('');
    expect(langFromExtension('/a/b/.zshrc')).toBe('');
  });
});

describe('resultLang', () => {
  it('Read → file language from the preview path', () => {
    expect(resultLang('Read', '/a/b/foo.py')).toBe('python');
    expect(resultLang('Read', '/a/b/foo.tsx')).toBe('tsx');
  });
  it('Read with an unknown extension → ""', () => {
    expect(resultLang('Read', '/a/b/foo.bin')).toBe('');
  });
  it('non-Read tools → "" regardless of arg', () => {
    expect(resultLang('Bash', 'ls -la')).toBe('');
    expect(resultLang('Grep', 'pattern')).toBe('');
    expect(resultLang(null, '/a/foo.py')).toBe('');
  });
  // Regression guard for the #177 S3 B4 note: the Edit/MultiEdit highlight
  // broadening is delivered by DiffCard calling fileLangForCall, NOT by widening
  // resultLang. Edit/MultiEdit must still get '' from resultLang.
  it('stays Read-scoped — Edit/MultiEdit are NOT widened', () => {
    expect(resultLang('Edit', '/a/b/foo.py')).toBe('');
    expect(resultLang('MultiEdit', '/a/b/foo.py')).toBe('');
  });
});

describe('fileLangForCall', () => {
  it('infers language from structured input.file_path', () => {
    expect(fileLangForCall({ name: 'Edit', input: { file_path: '/a/b.py' } })).toBe('python');
    expect(fileLangForCall({ name: 'MultiEdit', input: { file_path: '/x/y.tsx' } })).toBe('tsx');
    expect(fileLangForCall({ name: 'Write', input: { file_path: '/c/d.css' } })).toBe('css');
  });
  it('returns "" when there is no usable file_path', () => {
    expect(fileLangForCall({ name: 'Edit', input: null })).toBe('');
    expect(fileLangForCall({ name: 'Edit', input: {} })).toBe('');
    // Bash carries `command`, not `file_path` → plain.
    expect(fileLangForCall({ name: 'Bash', input: { command: 'ls' } })).toBe('');
    // Non-string file_path is ignored defensively.
    expect(fileLangForCall({ name: 'Edit', input: { file_path: 123 } })).toBe('');
    // Unknown extension degrades to plain.
    expect(fileLangForCall({ name: 'Edit', input: { file_path: '/a/b.xyz' } })).toBe('');
  });
});
