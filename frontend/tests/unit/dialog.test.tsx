import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

describe("Dialog", () => {
  it("body へ portal し、祖先 Card の stacking context 外に出る", async () => {
    render(
      <Card data-testid="host-card">
        <Dialog open onOpenChange={vi.fn()}>
          <DialogHeader>
            <DialogTitle>強制承認</DialogTitle>
          </DialogHeader>
          <DialogContent>理由入力</DialogContent>
          <DialogFooter>フッター</DialogFooter>
        </Dialog>
      </Card>,
    );

    const overlay = await screen.findByTestId("dialog-overlay");
    expect(overlay.parentElement).toBe(document.body);
    expect(document.body.contains(overlay)).toBe(true);
    expect(screen.getByTestId("host-card").contains(overlay)).toBe(false);
    expect(overlay.className).toMatch(/z-\[9999\]/);
    expect(overlay.firstElementChild?.className).toMatch(/bg-black\/80/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Escape とオーバーレイクリックで閉じる", async () => {
    const onOpenChange = vi.fn();
    render(
      <Dialog open onOpenChange={onOpenChange}>
        <DialogTitle>確認</DialogTitle>
      </Dialog>,
    );

    const overlay = await screen.findByTestId("dialog-overlay");
    fireEvent.click(overlay.firstElementChild as Element);
    expect(onOpenChange).toHaveBeenCalledWith(false);

    onOpenChange.mockClear();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });
});
