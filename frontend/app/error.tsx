"use client";

import { useEffect } from "react";

/**
 * 页面级错误边界：渲染期异常（例如模型产出的字段格式出乎意料）显示兜底说明，
 * 而不是整页白屏。注意重试会重新挂载页面，当前 run 的界面状态会重置（runId 还没进 URL）。
 */
export default function Error({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="mx-auto max-w-xl px-6 py-16 text-sm">
      <h1 className="text-base font-semibold">页面渲染出错了</h1>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">
        多半是模型返回的某个字段格式不符合预期，详细错误已输出到浏览器控制台。
        点「重试」会重新加载页面，当前的阅读进度会重置。
      </p>
      <button
        type="button"
        onClick={() => retry()}
        className="mt-4 rounded bg-neutral-900 px-3 py-1.5 text-white hover:bg-neutral-700 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300"
      >
        重试
      </button>
    </main>
  );
}
