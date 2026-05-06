import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Footer } from '../src/components/Footer';

describe('<Footer />', () => {
  it('renders 6 items: ↑/↓ scroll, tab, r, s, ?, q', () => {
    render(<Footer />);
    expect(screen.getByText(/scroll/)).toBeInTheDocument();
    expect(screen.getByText(/next panel/)).toBeInTheDocument();
    expect(screen.getByText('refresh')).toBeInTheDocument();
    expect(screen.getByText('settings')).toBeInTheDocument();
    expect(screen.getByText('help')).toBeInTheDocument();
    expect(screen.getByText('quit')).toBeInTheDocument();
  });

  it('renders r, s, ?, q as .kb-btn buttons', () => {
    render(<Footer />);
    const btns = document.querySelectorAll('.footer .kb-btn');
    expect(btns.length).toBe(4);
    // Verify ids on the four clickable pills
    expect(document.getElementById('footer-r')).not.toBeNull();
    expect(document.getElementById('footer-s')).not.toBeNull();
    expect(document.getElementById('footer-help')).not.toBeNull();
    expect(document.getElementById('footer-q')).not.toBeNull();
  });

  it('applies accent-green to tab, accent-purple to r, accent-amber to ?', () => {
    render(<Footer />);
    const tabKbd = document.querySelector('kbd.accent-green');
    expect(tabKbd?.textContent).toBe('tab');
    const rKbd = document.querySelector('kbd.accent-purple');
    expect(rKbd?.textContent).toBe('r');
    const helpKbd = document.querySelector('kbd.accent-amber');
    expect(helpKbd?.textContent).toBe('?');
  });
});
