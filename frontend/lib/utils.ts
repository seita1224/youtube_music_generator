import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

// shadcn/ui 標準の className マージユーティリティ。
// clsx で条件結合 → tailwind-merge で衝突する Tailwind クラスを解決する。
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
