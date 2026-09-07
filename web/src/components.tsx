import {
  Children,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type { CSSProperties, ReactNode } from "react";
import { Check, ChevronDown, Copy, Info, X } from "lucide-react";
import type { RecordData } from "./api";

export function Logo({ small = false }: { small?: boolean }) {
  return (
    <span className={`logo ${small ? "small" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 32 32">
        <path d="M8 5v22M24 5v22M8 16h16" />
        <circle cx="16" cy="16" r="3" />
      </svg>
    </span>
  );
}
export function Badge({
  children,
  tone = "neutral",
  dot = true,
}: {
  children: ReactNode;
  tone?: string;
  dot?: boolean;
}) {
  return (
    <span className={`badge ${tone}`}>
      {dot && <i />}
      {children}
    </span>
  );
}
export function Avatar({ bot, size = "" }: { bot: RecordData; size?: string }) {
  return (
    <span
      className={`avatar ${size}`}
      style={{ "--avatar-color": bot.color || "#b9de89" } as CSSProperties}
    >
      {bot.name?.slice(0, 1) || "B"}
      {bot.role === "hortator" && <span className="avatar-crown">✦</span>}
    </span>
  );
}
export function Empty({
  title,
  children,
  icon,
  action,
}: {
  title: string;
  children?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon && <span className="empty-icon">{icon}</span>}
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  const hintId = useId();
  return (
    <label className={`field ${className}`}>
      <span className="field-label">{label}</span>
      {Children.map(children, (child) =>
        isValidElement<Record<string, unknown>>(child) &&
        ["input", "textarea", "select"].includes(String(child.type))
          ? cloneElement(child, {
              "aria-label": label,
              "aria-describedby": hint ? hintId : undefined,
            })
          : child,
      )}
      {hint && <small id={hintId}>{hint}</small>}
    </label>
  );
}
export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <label className="switch-label">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
        aria-label={label}
      />
      <span className="switch-track" />
      <span>{label}</span>
    </label>
  );
}
export function Code({
  value,
  label = "JSON",
  expanded = false,
}: {
  value: any;
  label?: string;
  expanded?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <details className="code-block" open={expanded}>
      <summary>
        <span>
          <ChevronDown size={13} />
          {label}
        </span>
        <button
          className="icon-button"
          type="button"
          aria-label={`Copy ${label}`}
          onClick={(e) => {
            e.preventDefault();
            navigator.clipboard
              .writeText(text)
              .then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
              })
              .catch(() => setCopied(false));
          }}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
      </summary>
      <pre>{text}</pre>
    </details>
  );
}
export function JsonInput({
  label,
  value,
  onChange,
  hint,
  onValidityChange,
}: {
  label: string;
  value: any;
  onChange: (value: RecordData) => void;
  hint?: string;
  onValidityChange?: (valid: boolean) => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const [raw, setRaw] = useState(JSON.stringify(value || {}, null, 2));
  const [error, setError] = useState("");
  const accepted = useRef(JSON.stringify(value || {}));
  useEffect(() => {
    const serialized = JSON.stringify(value || {});
    if (serialized !== accepted.current) {
      accepted.current = serialized;
      setRaw(JSON.stringify(value || {}, null, 2));
      setError("");
      input.current?.setCustomValidity("");
      onValidityChange?.(true);
    }
  }, [value, onValidityChange]);
  return (
    <Field label={label} hint={hint}>
      <textarea
        ref={input}
        className={`json-input ${error ? "invalid" : ""}`}
        rows={10}
        spellCheck={false}
        value={raw}
        onChange={(e) => {
          setRaw(e.target.value);
          try {
            const parsed = JSON.parse(e.target.value);
            if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
              throw new Error("Use a JSON object");
            setError("");
            onValidityChange?.(true);
            accepted.current = JSON.stringify(parsed);
            onChange(parsed);
            e.target.setCustomValidity("");
          } catch (err) {
            const message = err instanceof Error ? err.message : "Invalid JSON";
            setError(message);
            onValidityChange?.(false);
            e.target.setCustomValidity(message);
          }
        }}
      />
      {error && <span className="field-error">{error}</span>}
    </Field>
  );
}
export function Notice({
  children,
  warning = false,
}: {
  children: ReactNode;
  warning?: boolean;
}) {
  return (
    <div className={`notice ${warning ? "warning" : ""}`}>
      <Info size={16} />
      <span>{children}</span>
    </div>
  );
}
export function Metric({
  label,
  value,
  sub,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub: ReactNode;
  icon: ReactNode;
}) {
  return (
    <div className="metric">
      <div className="metric-label">
        {label}
        <span>{icon}</span>
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-sub">{sub}</div>
    </div>
  );
}
export function Modal({
  title,
  subtitle,
  children,
  close,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  close: () => void;
  wide?: boolean;
}) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const el = dialog.current;
    el?.focus();
    const bodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key === "Tab" && el) {
        const items = Array.from(
          el.querySelectorAll<HTMLElement>(
            'button, input, select, textarea, a[href], summary, [tabindex="0"]',
          ),
        ).filter(
          (x) => !x.hasAttribute("disabled") && x.getClientRects().length,
        );
        const first = items[0],
          last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.body.style.overflow = bodyOverflow;
      document.removeEventListener("keydown", handler);
      prev?.focus();
    };
  }, []);
  return (
    <div className="modal-shade">
      <div
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        tabIndex={-1}
        ref={dialog}
      >
        <header className="modal-header">
          <div>
            <span className="eyebrow">COUNCIL CONFIGURATION</span>
            <h2 id={id}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button
            className="icon-button"
            onClick={close}
            aria-label="Close dialog"
          >
            <X size={20} />
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
