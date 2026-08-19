import { DashboardStreamHub } from '../store/dashboardStreamHub';

interface SharedWorkerConnectEvent extends Event {
  ports: MessagePort[];
}

interface SharedWorkerGlobal {
  onconnect: ((event: SharedWorkerConnectEvent) => void) | null;
}

const hub = new DashboardStreamHub(() => new EventSource('/api/events'));
const workerGlobal = globalThis as unknown as SharedWorkerGlobal;

workerGlobal.onconnect = (event) => {
  for (const port of event.ports) hub.connect(port);
};
