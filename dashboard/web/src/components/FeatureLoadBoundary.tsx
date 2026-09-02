import { Component, Suspense, type ErrorInfo, type ReactNode } from 'react';

interface FeatureLoadBoundaryProps {
  children: ReactNode;
  name: string;
}

interface FeatureLoadBoundaryState {
  failed: boolean;
}

export class FeatureLoadBoundary extends Component<
  FeatureLoadBoundaryProps,
  FeatureLoadBoundaryState
> {
  state: FeatureLoadBoundaryState = { failed: false };

  static getDerivedStateFromError(): FeatureLoadBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`deferred ${this.props.name} failed to load`, error, info);
  }

  render() {
    const { children, name } = this.props;
    if (this.state.failed) {
      return (
        <div
          className="modal-backdrop feature-load-failure"
          role="alert"
          aria-label={`${name} failed to load`}
        >
          <section className="modal-card feature-load-failure-card">
            <h2>{name} could not load</h2>
            <p>
              The dashboard may have updated while this tab was open. Reload
              to use one coherent build.
            </p>
            <button type="button" onClick={() => window.location.reload()}>
              Reload dashboard
            </button>
          </section>
        </div>
      );
    }
    return (
      <Suspense
        fallback={(
          <div className="feature-load-pending" role="status">
            Loading {name}…
          </div>
        )}
      >
        {children}
      </Suspense>
    );
  }
}
