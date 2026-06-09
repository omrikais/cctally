import { describe, expect, it } from 'vitest';
import { langFromExtension, resultLang } from './toolLang';

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
});
