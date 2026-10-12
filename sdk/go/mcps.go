// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

package openviking

import (
	"context"
	"net/http"
	"net/url"
)

// AddMCP registers a Streamable HTTP MCP server and discovers its tools.
func (c *Client) AddMCP(ctx context.Context, name string, serverURL string, headers map[string]string) (map[string]any, error) {
	if headers == nil {
		headers = map[string]string{}
	}
	payload := map[string]any{
		"name":    name,
		"url":     serverURL,
		"headers": headers,
	}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps", nil, payload, &result)
	return result, err
}

// RemoveMCP removes an MCP server and its tool catalog.
func (c *Client) RemoveMCP(ctx context.Context, name string) (map[string]any, error) {
	var result map[string]any
	err := c.doJSON(ctx, http.MethodDelete, "/api/v1/mcps/"+url.PathEscape(name), nil, nil, &result)
	return result, err
}

// ListMCPs lists registered MCP servers.
func (c *Client) ListMCPs(ctx context.Context) (map[string]any, error) {
	var result map[string]any
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/mcps", nil, nil, &result)
	return result, err
}

// FindMCPTools searches the MCP tool catalog semantically.
func (c *Client) FindMCPTools(ctx context.Context, queryText string, limit int) (map[string]any, error) {
	if limit == 0 {
		limit = 10
	}
	payload := map[string]any{"query": queryText, "limit": limit}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps/find", nil, payload, &result)
	return result, err
}

// ValidateMCP validates and discovers an MCP server without registering it.
func (c *Client) ValidateMCP(ctx context.Context, name string, serverURL string, headers map[string]string) (map[string]any, error) {
	if headers == nil {
		headers = map[string]string{}
	}
	payload := map[string]any{
		"name":    name,
		"url":     serverURL,
		"headers": headers,
	}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps/validate", nil, payload, &result)
	return result, err
}

// GetMCP returns a registered MCP server and its current tool catalog.
func (c *Client) GetMCP(ctx context.Context, name string) (map[string]any, error) {
	var result map[string]any
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/mcps/"+url.PathEscape(name), nil, nil, &result)
	return result, err
}

// SyncMCP refreshes an MCP tool catalog and reports affected live context mounts.
func (c *Client) SyncMCP(ctx context.Context, name string) (map[string]any, error) {
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps/"+url.PathEscape(name)+"/sync", nil, nil, &result)
	return result, err
}

// GetMCPStatus checks whether an MCP server is reachable.
func (c *Client) GetMCPStatus(ctx context.Context, name string) (map[string]any, error) {
	query := url.Values{"name": []string{name}}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/mcps/status", query, nil, &result)
	return result, err
}

// InvokeMCPTool invokes a registered MCP tool through OpenViking.
func (c *Client) InvokeMCPTool(ctx context.Context, toolURI string, arguments map[string]any) (map[string]any, error) {
	if arguments == nil {
		arguments = map[string]any{}
	}
	payload := map[string]any{
		"uri":       NormalizeURI(toolURI),
		"arguments": arguments,
	}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps/invoke", nil, payload, &result)
	return result, err
}

// MountMCPTool creates or replaces a live context node backed by an MCP tool.
func (c *Client) MountMCPTool(ctx context.Context, toolURI string, nodeURI string, title string, opts *MountMCPToolOptions) (map[string]any, error) {
	timeoutSeconds := 60
	arguments := map[string]any{}
	var description *string
	if opts != nil {
		description = opts.Description
		if opts.TimeoutSeconds != 0 {
			timeoutSeconds = opts.TimeoutSeconds
		}
		if opts.Arguments != nil {
			arguments = opts.Arguments
		}
	}
	payload := map[string]any{
		"tool_uri":        NormalizeURI(toolURI),
		"node_uri":        NormalizeURI(nodeURI),
		"title":           title,
		"description":     description,
		"timeout_seconds": timeoutSeconds,
		"arguments":       arguments,
	}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodPost, "/api/v1/mcps/mount", nil, payload, &result)
	return result, err
}

// UnmountMCPTool removes a live context mount and its node.
func (c *Client) UnmountMCPTool(ctx context.Context, nodeURI string) (map[string]any, error) {
	query := url.Values{"node_uri": []string{NormalizeURI(nodeURI)}}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodDelete, "/api/v1/mcps/mount", query, nil, &result)
	return result, err
}

// ListMCPMounts lists live context mounts for one MCP server and optional tool.
func (c *Client) ListMCPMounts(ctx context.Context, mcp string, tool string) (map[string]any, error) {
	query := url.Values{"mcp": []string{mcp}}
	if tool != "" {
		query.Set("tool", tool)
	}
	var result map[string]any
	err := c.doJSON(ctx, http.MethodGet, "/api/v1/mcps/mounts", query, nil, &result)
	return result, err
}
