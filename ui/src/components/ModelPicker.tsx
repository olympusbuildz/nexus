import { backendLabel } from '../api/status';
import { useApp } from '../state/context';

export default function ModelPicker({ disabled = false }: { disabled?: boolean }) {
  const { models, selected, select } = useApp();
  const value = selected ? `${selected.backend}\n${selected.model}` : '';
  return (
    <label className="flex-1 min-w-0">
      <span className="sr-only">Model</span>
      <select
        className="input w-full"
        value={value}
        disabled={disabled || models.length === 0}
        onChange={(e) => {
          const [backend = '', model = ''] = e.target.value.split('\n');
          select({ backend, model });
        }}
      >
        {models.length === 0 && <option value="">No model can run yet</option>}
        {models.map((m) => (
          <option key={`${m.backend}\n${m.id}`} value={`${m.backend}\n${m.id}`}>
            {m.id} — {backendLabel(m.backend)}
          </option>
        ))}
      </select>
    </label>
  );
}
