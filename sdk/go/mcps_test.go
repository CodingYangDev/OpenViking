// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

package openviking

import (
	"context"
	"net/http"
	"reflect"
	"testing"
)

func TestMCPOperationsMatchServerContract(t *testing.T) {
	type expectedRequest struct {
		method string
		path   string
		query  map[string]string
		body   map[string]any
	}
	expected := []expectedRequest{
		{
			method: http.MethodPost,
			path:   "/api/v1/mcps",
			body: map[string]any{
				"name":    "demo",
				"url":     "https://mcp.example.com/api",
				"headers": map[string]any{"Authorization": "Bearer secret"},
			},
		},
		{method: http.MethodDelete, path: "/api/v1/mcps/demo"},
		{method: http.MethodGet, path: "/api/v1/mcps"},
		{
			method: http.MethodPost,
			path:   "/api/v1/mcps/find",
			body:   map[string]any{"query": "weather forecast", "limit": float64(5)},
		},
		{
			method: http.MethodPost,
			path:   "/api/v1/mcps/validate",
			body: map[string]any{
				"name":    "demo",
				"url":     "https://mcp.example.com/api",
				"headers": map[string]any{},
			},
		},
		{method: http.MethodGet, path: "/api/v1/mcps/demo"},
		{method: http.MethodPost, path: "/api/v1/mcps/demo/sync"},
		{
			method: http.MethodGet,
			path:   "/api/v1/mcps/status",
			query:  map[string]string{"name": "demo"},
		},
		{
			method: http.MethodPost,
			path:   "/api/v1/mcps/invoke",
			body: map[string]any{
				"uri":       "viking://agent/tools/demo/current.json",
				"arguments": map[string]any{"city": "Beijing"},
			},
		},
		{
			method: http.MethodPost,
			path:   "/api/v1/mcps/mount",
			body: map[string]any{
				"tool_uri":        "viking://agent/tools/demo/current.json",
				"node_uri":        "viking://resources/demo/weather.md",
				"title":           "Beijing Weather",
				"description":     "Current weather in Beijing",
				"timeout_seconds": float64(120),
				"arguments":       map[string]any{"city": "Beijing"},
			},
		},
		{
			method: http.MethodDelete,
			path:   "/api/v1/mcps/mount",
			query:  map[string]string{"node_uri": "viking://resources/demo/weather.md"},
		},
		{
			method: http.MethodGet,
			path:   "/api/v1/mcps/mounts",
			query:  map[string]string{"mcp": "demo", "tool": "current"},
		},
	}
	requestIndex := 0
	client, closeServer := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requestIndex >= len(expected) {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		want := expected[requestIndex]
		requestIndex++
		if r.Method != want.method || r.URL.Path != want.path {
			t.Fatalf("request = %s %s, want %s %s", r.Method, r.URL.Path, want.method, want.path)
		}
		for key, value := range want.query {
			if got := r.URL.Query().Get(key); got != value {
				t.Fatalf("query %s = %q, want %q", key, got, value)
			}
		}
		if want.body != nil {
			if got := readJSONBody(t, r); !reflect.DeepEqual(got, want.body) {
				t.Fatalf("body = %#v, want %#v", got, want.body)
			}
		}
		writeOK(t, w, map[string]any{"ok": true})
	}))
	defer closeServer()

	ctx := context.Background()
	if _, err := client.AddMCP(ctx, "demo", "https://mcp.example.com/api", map[string]string{
		"Authorization": "Bearer secret",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.RemoveMCP(ctx, "demo"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListMCPs(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := client.FindMCPTools(ctx, "weather forecast", 5); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ValidateMCP(ctx, "demo", "https://mcp.example.com/api", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetMCP(ctx, "demo"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.SyncMCP(ctx, "demo"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetMCPStatus(ctx, "demo"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.InvokeMCPTool(ctx, "agent/tools/demo/current.json", map[string]any{
		"city": "Beijing",
	}); err != nil {
		t.Fatal(err)
	}
	description := "Current weather in Beijing"
	if _, err := client.MountMCPTool(
		ctx,
		"agent/tools/demo/current.json",
		"resources/demo/weather.md",
		"Beijing Weather",
		&MountMCPToolOptions{
			Description:    &description,
			TimeoutSeconds: 120,
			Arguments:      map[string]any{"city": "Beijing"},
		},
	); err != nil {
		t.Fatal(err)
	}
	if _, err := client.UnmountMCPTool(ctx, "resources/demo/weather.md"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListMCPMounts(ctx, "demo", "current"); err != nil {
		t.Fatal(err)
	}
	if requestIndex != len(expected) {
		t.Fatalf("received %d requests, want %d", requestIndex, len(expected))
	}
}

func TestMCPDefaultsMatchServerContract(t *testing.T) {
	requestIndex := 0
	client, closeServer := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := readJSONBody(t, r)
		switch requestIndex {
		case 0:
			if body["limit"] != float64(10) {
				t.Fatalf("limit = %#v", body["limit"])
			}
		case 1:
			if !reflect.DeepEqual(body["arguments"], map[string]any{}) {
				t.Fatalf("invoke arguments = %#v", body["arguments"])
			}
		case 2:
			if body["timeout_seconds"] != float64(60) {
				t.Fatalf("timeout_seconds = %#v", body["timeout_seconds"])
			}
			if body["description"] != nil {
				t.Fatalf("description = %#v", body["description"])
			}
			if !reflect.DeepEqual(body["arguments"], map[string]any{}) {
				t.Fatalf("mount arguments = %#v", body["arguments"])
			}
		default:
			t.Fatalf("unexpected request")
		}
		requestIndex++
		writeOK(t, w, map[string]any{})
	}))
	defer closeServer()

	ctx := context.Background()
	if _, err := client.FindMCPTools(ctx, "weather", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := client.InvokeMCPTool(ctx, "viking://agent/tools/demo/current.json", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := client.MountMCPTool(
		ctx,
		"viking://agent/tools/demo/current.json",
		"viking://resources/demo/weather.md",
		"Weather",
		nil,
	); err != nil {
		t.Fatal(err)
	}
}
