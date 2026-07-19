// backend `/posts` の型付きクライアント(contracts/backend-api.yaml の Post / AudioTrack)。
//
// 型は schema.ts 生成(`npm run gen:api`)には依存せず、 contract schema に
// 手書きで一致させる。 `audio_uri` はレスポンスに露出せず、 再生/DL は
// 認証付きパス(postTrackAudioPath / postTrackDownloadPath)経由。

import { apiGet } from "@/lib/api/client";
import { authFetch } from "@/lib/auth";

export type PostStatus =
  | "pending"
  | "generating"
  | "music_generated"
  | "generated"
  | "posting"
  | "posted"
  | "failed";

export type AcoustidStatus = "not_checked" | "clear" | "hit" | "api_error";

export interface Post {
  readonly id: string; // uuid
  readonly plan_id: string; // uuid
  readonly position: number;
  readonly genre: string;
  readonly payload?: Record<string, unknown>;
  readonly status: PostStatus;
  readonly final_title?: string;
  readonly final_description?: string;
  readonly thumbnail_uri?: string;
  readonly video_uri?: string;
  readonly youtube_video_id?: string;
  readonly scheduled_at?: string;
  readonly posted_at?: string;
  readonly retention_24h?: number;
  readonly views_24h?: number;
  readonly error_category?: string;
  readonly error_message?: string;
  readonly created_at: string; // date-time
}

export interface PostListResponse {
  readonly items: readonly Post[];
}

/** AudioTrack メタ。 audio_uri は含めない(contract)。 */
export interface AudioTrack {
  readonly id: string; // uuid
  readonly post_id: string; // uuid
  readonly position: number; // 0..5
  readonly duration_sec: number;
  readonly bpm?: number | null;
  readonly music_key?: string | null;
  readonly subtheme?: string | null;
  readonly acoustid_status: AcoustidStatus;
  readonly regenerated_count?: number;
  readonly generated_at: string; // date-time
}

export interface AudioTrackListResponse {
  readonly items: readonly AudioTrack[];
}

/** Plan に属する Post 一覧を取得する。 */
export async function listPosts(planId: string): Promise<PostListResponse> {
  const params = new URLSearchParams({ plan_id: planId });
  return apiGet<PostListResponse>(`/posts?${params.toString()}`);
}

/** Post の AudioTrack メタ一覧を取得する。 */
export async function listPostTracks(
  postId: string,
): Promise<AudioTrackListResponse> {
  return apiGet<AudioTrackListResponse>(`/posts/${postId}/tracks`);
}

/** ブラウザ再生用 WAV の backend パス(authFetch / useAuthedBlobUrl 用)。 */
export function postTrackAudioPath(postId: string, position: number): string {
  return `/posts/${postId}/tracks/${position}/audio`;
}

/** ダウンロード用 WAV の backend パス(authFetch 用)。 */
export function postTrackDownloadPath(
  postId: string,
  position: number,
): string {
  return `/posts/${postId}/tracks/${position}/download`;
}

/**
 * 認証付きでトラック WAV を取得し、 ブラウザのダウンロードを発火する。
 * Content-Disposition の filename があればそれを使い、 無ければ既定名。
 */
export async function downloadPostTrack(
  postId: string,
  position: number,
): Promise<void> {
  const response = await authFetch(postTrackDownloadPath(postId, position));
  if (!response.ok) {
    throw new Error(`download failed: ${response.status}`);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition");
  const matched = disposition?.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
  const filename =
    matched?.[1] != null
      ? decodeURIComponent(matched[1].replace(/"/g, ""))
      : `track-${position}.wav`;
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}
