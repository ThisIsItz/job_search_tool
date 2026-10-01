"use client";

import {Suspense, useEffect, useState} from "react";
import {useSearchParams} from "next/navigation";

function ApplyForm() {
  const searchParams = useSearchParams();

  const [input, setInput] = useState("");
  const [wantCoverLetter, setWantCoverLetter] = useState(true);
  const [questionsText, setQuestionsText] = useState("");
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const url = searchParams.get("url");
    if (url) setInput(url);
  }, [searchParams]);

  async function generate() {
    setLoading(true);
    setError(null);
    setPrompt("");
    setCopied(false);
    try {
      const questions = questionsText
        .split("\n")
        .map((q) => q.trim())
        .filter(Boolean);
      const res = await fetch("/api/apply-prompt", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({input, wantCoverLetter, questions}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      setPrompt(data.prompt);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function copyPrompt() {
    await navigator.clipboard.writeText(prompt);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  function downloadPrompt() {
    const blob = new Blob([prompt], {type: "text/markdown"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "prompt_for_chat.md";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="apply-page">
      <h1>Preparar candidatura</h1>
      <p className="subtitle">
        Pega la oferta y genero un prompt listo para copiar en claude.ai o ChatGPT — gratis, usa tu
        suscripción, no la API de pago.
      </p>

      <label className="apply-field">
        Oferta (URL ya guardada en tu búsqueda, o el texto completo del puesto pegado)
        <textarea
          rows={8}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="https://... o pega aquí la descripción del puesto"
        />
      </label>

      <label className="apply-checkbox">
        <input
          type="checkbox"
          checked={wantCoverLetter}
          onChange={(e) => setWantCoverLetter(e.target.checked)}
        />
        Quiero carta de presentación
      </label>

      <label className="apply-field">
        Preguntas de la aplicación (opcional, una por línea)
        <textarea
          rows={4}
          value={questionsText}
          onChange={(e) => setQuestionsText(e.target.value)}
          placeholder={"¿Por qué quieres trabajar aquí?\n¿Expectativa salarial?"}
        />
      </label>

      <button className="btn" onClick={generate} disabled={loading || !input.trim()}>
        {loading ? "Generando..." : "Generar prompt"}
      </button>

      {error && <p className="error-state">{error}</p>}

      {prompt && (
        <div className="apply-result">
          <div className="apply-result-actions">
            <button className="btn secondary" onClick={copyPrompt}>
              {copied ? "Copiado ✓" : "Copiar al portapapeles"}
            </button>
            <button className="btn secondary" onClick={downloadPrompt}>
              Descargar .md
            </button>
          </div>
          <textarea className="apply-prompt-output" readOnly rows={20} value={prompt} />
          <p className="subtitle">
            Pega esto en claude.ai o ChatGPT, y guarda la respuesta donde prefieras.
          </p>
        </div>
      )}
    </main>
  );
}

export default function ApplyPage() {
  return (
    <Suspense fallback={<main className="apply-page"><p className="loading-state">Loading…</p></main>}>
      <ApplyForm />
    </Suspense>
  );
}
