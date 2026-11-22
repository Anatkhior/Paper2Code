"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { PDFDocumentProxy, RenderTask, TextLayer } from "pdfjs-dist";

interface Props {
  url: string;
  page: number;
  fallbackUrl?: string | null;
  onReady?: () => void;
  children?: ReactNode;
}

/** 原 PDF 的 canvas 与官方文字层共用 viewport；不把论文送到外部渲染服务。 */
export default function PdfPage({ url, page, fallbackUrl, onReady, children }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const slot = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [document, setDocument] = useState<{ url: string; value: PDFDocumentProxy } | null>(null);
  const [loadError, setLoadError] = useState<{ url: string; message: string } | null>(null);
  const [rendered, setRendered] = useState<{ key: string; url: string; page: number; hasText: boolean; error?: string } | null>(null);
  const [fallbackLoaded, setFallbackLoaded] = useState<string | null>(null);
  const imageKey = JSON.stringify([url, page, fallbackUrl]);
  const key = JSON.stringify([url, page, width]);
  const error = loadError?.url === url ? loadError.message : rendered?.key === key ? rendered.error : null;
  const ready = rendered?.key === key && !error;
  const samePagePainted = rendered?.url === url && rendered?.page === page && !rendered.error;

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let task: ReturnType<typeof import("pdfjs-dist").getDocument> | undefined;
    void import("pdfjs-dist").then(async (pdfjs) => {
      if (cancelled) return;
      pdfjs.GlobalWorkerOptions.workerSrc = `/pdfjs/pdf.worker.min.mjs?v=${pdfjs.version}`;
      task = pdfjs.getDocument({
        url, cMapUrl: "/pdfjs/cmaps/", cMapPacked: true,
        standardFontDataUrl: "/pdfjs/standard_fonts/", wasmUrl: "/pdfjs/wasm/", iccUrl: "/pdfjs/iccs/",
      });
      const value = await task.promise;
      if (!cancelled) setDocument({ url, value });
    }).catch((caught: unknown) => {
      if (!cancelled) setLoadError({ url, message: String(caught) });
    });
    return () => { cancelled = true; void task?.destroy(); };
  }, [url]);

  useEffect(() => {
    if (document?.url !== url || width <= 0 || !slot.current) return;
    let cancelled = false;
    let canvasTask: RenderTask | undefined;
    let textTask: TextLayer | undefined;
    const container = slot.current;
    // 换页或换文档时先清空；同页缩放/重排保留上一帧画布，待新帧画完再原子替换，避免闪烁。
    if (container.dataset.renderedPage !== `${url}#${page}`) {
      container.replaceChildren();
    }
    void (async () => {
      const [pdfjs, pdfPage] = await Promise.all([import("pdfjs-dist"), document.value.getPage(page)]);
      if (cancelled) return;
      const natural = pdfPage.getViewport({ scale: 1 });
      const viewport = pdfPage.getViewport({ scale: width / natural.width });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const canvas = window.document.createElement("canvas");
      canvas.width = Math.ceil(viewport.width * ratio);
      canvas.height = Math.ceil(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      canvas.setAttribute("aria-hidden", "true");
      const layer = window.document.createElement("div");
      layer.className = "textLayer";
      layer.setAttribute("aria-label", `第 ${page} 页可选原文`);
      layer.style.setProperty("--total-scale-factor", String(viewport.scale));
      layer.style.setProperty("--scale-round-x", "1px");
      layer.style.setProperty("--scale-round-y", "1px");
      const content = await pdfPage.getTextContent();
      if (cancelled) return;
      textTask = new pdfjs.TextLayer({ textContentSource: content, container: layer, viewport });
      canvasTask = pdfPage.render({ canvas, viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
      await Promise.all([canvasTask.promise, textTask.render()]);
      if (cancelled) return;
      container.style.width = `${viewport.width}px`;
      container.style.height = `${viewport.height}px`;
      container.dataset.renderedPage = `${url}#${page}`;
      container.replaceChildren(canvas, layer);
      setRendered({ key, url, page, hasText: content.items.some((item) => "str" in item && item.str.trim()) });
      onReady?.();
    })().catch((caught: unknown) => {
      if (!cancelled) setRendered({ key, url, page, hasText: false, error: String(caught) });
    });
    return () => { cancelled = true; canvasTask?.cancel(); textTask?.cancel(); };
  }, [document, url, page, width, key, onReady]);

  return <div ref={host} className="pdf-page" data-page={page} aria-busy={!ready && !error}>
    {!ready && !samePagePainted && !error && <p role="status" className="pdf-page-notice">正在加载 PDF 原版及文字层…</p>}
    {error && <p role="alert" className="pdf-page-notice">文字层加载失败。正在使用页面图像，也可切换原文文本或打开 PDF。</p>}
    <div className="pdf-page-surface relative">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      {error && fallbackUrl && <img key={imageKey} src={fallbackUrl} alt={`论文第 ${page} 页`} className="block h-auto w-full" onLoad={() => { setFallbackLoaded(imageKey); onReady?.(); }} />}
      <div ref={slot} className="pdf-page-layers" hidden={Boolean(error)} />
      {(ready || samePagePainted || (error && fallbackLoaded === imageKey)) && children}
    </div>
    {ready && !rendered.hasText && <p role="status" className="pdf-page-notice">本页没有可选择文字（可能是扫描页），暂不支持直接划选。</p>}
  </div>;
}
