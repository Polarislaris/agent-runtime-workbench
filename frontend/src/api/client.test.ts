import {afterEach, describe, expect, it, vi} from "vitest";

import {ApiError, createRun, listRunHistory, resolvePermission} from "./client";


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("runtime API client", () => {
  it("serializes createRun and returns the typed JSON body", async () => {
    const snapshot = {id: "run_1", status: "queued"};
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify(snapshot),
      {status: 201, headers: {"Content-Type": "application/json"}},
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(createRun("hello")).resolves.toEqual(snapshot);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({prompt: "hello"}),
      }),
    );
  });

  it("normalizes non-2xx responses and accepts permission 204", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(
        JSON.stringify({detail: "run not found"}),
        {status: 404, headers: {"Content-Type": "application/json"}},
      ))
      .mockResolvedValueOnce(new Response(null, {status: 204}));
    vi.stubGlobal("fetch", fetchMock);

    const error = await createRun("hello").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({status: 404, message: "run not found"});

    await expect(
      resolvePermission("run_1", "perm_1", "allow"),
    ).resolves.toBeUndefined();
  });

  it("requests a filtered history page and reads its cursor header", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify([{id: "run_1", status: "completed"}]),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Next-Cursor": "cursor_next",
        },
      },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listRunHistory({
      status: "completed",
      cursor: "cursor old",
      limit: 25,
      signal: controller.signal,
    })).resolves.toEqual({
      items: [{id: "run_1", status: "completed"}],
      nextCursor: "cursor_next",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs?status=completed&cursor=cursor+old&limit=25",
      expect.objectContaining({signal: controller.signal}),
    );
  });
});
