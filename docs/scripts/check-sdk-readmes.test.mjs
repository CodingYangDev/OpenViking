import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { checkSDKReadmes } from './check-sdk-readmes.mjs'

const requiredSections = [
  'SDK 定位',
  '安装',
  '初始化',
  '五分钟跑通',
  '核心能力',
  '错误处理',
  'API 速查表',
  '兼容性',
]
const requiredContractSubsections = ['方法契约', '字段契约']

function withFixture(buildFixture, assertion) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'openviking-sdk-readmes-'))
  try {
    buildFixture(root)
    assertion(root)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function writeReadme(
  root,
  language,
  sections = requiredSections,
  contractSubsections = requiredContractSubsections,
) {
  const directory = path.join(root, 'sdk', language)
  fs.mkdirSync(directory, { recursive: true })
  fs.writeFileSync(
    path.join(directory, 'README.md'),
    `# OpenViking ${language} SDK\n\n这是中文 SDK 使用文档，用于说明安装、认证、任务、错误处理和兼容性。`.repeat(4) +
      `\n\n${sections
        .flatMap((section) => [
          `## ${section}`,
          ...(section === 'API 速查表'
            ? contractSubsections.map((subsection) => (
              subsection === '方法契约'
                ? `### ${subsection}\n\n| 方法 | 参数 | 说明 |\n| --- | --- | --- |\n| 示例 | 参数 | 方法说明 |`
                : `### ${subsection}\n\n| 字段 | 说明 |\n| --- | --- |\n| 示例 | 字段契约说明 |`
            ))
            : []),
        ])
        .join('\n\n')}\n`,
  )
}

function addToMethodContract(root, language, content) {
  const readme = path.join(root, 'sdk', language, 'README.md')
  const source = fs.readFileSync(readme, 'utf8')
  fs.writeFileSync(readme, source.replace('### 方法契约', `### 方法契约\n\n${content}`))
}

function writeClientSource(root, language, source) {
  const filename = language === 'go'
    ? path.join(root, 'sdk', 'go', 'client.go')
    : path.join(
      root,
      'sdk',
      language,
      language === 'python' ? 'openviking_sdk/client.py' : 'src/client.ts',
    )
  fs.mkdirSync(path.dirname(filename), { recursive: true })
  fs.writeFileSync(filename, source)
}

function writeEmptyClientSources(root) {
  writeClientSource(root, 'go', 'package openviking\n\ntype Client struct{}\n')
  writeClientSource(root, 'python', 'class SyncHTTPClient:\n    pass\n')
  writeClientSource(root, 'typescript', 'export class OpenVikingClient {}\n')
}

test('reports duplicate localized readmes and missing user-guide sections', () => {
  withFixture(
    (root) => {
      writeReadme(root, 'go', ['SDK 定位'])
      fs.writeFileSync(path.join(root, 'sdk', 'go', 'README_CN.md'), '# 重复文档\n')
      writeReadme(root, 'python')
      writeReadme(root, 'typescript')
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README_CN.md')))
      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('安装')))
    },
  )
})

test('accepts one Chinese complete README for every supported SDK', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
    },
    (root) => assert.deepEqual(checkSDKReadmes(root), []),
  )
})

test('requires method and field contracts beneath the API quick reference', () => {
  withFixture(
    (root) => {
      writeReadme(root, 'go', requiredSections, [])
      writeReadme(root, 'python')
      writeReadme(root, 'typescript')
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('方法契约')))
      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('字段契约')))
    },
  )
})

test('reports public client methods that are absent from the corresponding README', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeClientSource(
        root,
        'go',
        'package openviking\n\ntype Client struct{}\n\nfunc (c *Client) Documented() {}\nfunc (c *Client) Undocumented() {}\n',
      )
      writeClientSource(
        root,
        'python',
        'class SyncHTTPClient:\n    def documented(self):\n        pass\n\n    def undocumented(self):\n        pass\n',
      )
      writeClientSource(
        root,
        'typescript',
        'export class OpenVikingClient {\n  async documented() {}\n  async undocumented() {}\n}\n',
      )
      addToMethodContract(root, 'go', '`Documented`')
      addToMethodContract(root, 'python', '`documented`')
      addToMethodContract(root, 'typescript', '`documented`')
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('Undocumented')))
      assert.ok(!errors.some((error) => error.includes('sdk/go/README.md') && error.includes('Documented')))
      assert.ok(errors.some((error) => error.includes('sdk/python/README.md') && error.includes('undocumented')))
      assert.ok(!errors.some((error) => error.includes('sdk/python/README.md') && error.includes('"documented" is not documented')))
      assert.ok(errors.some((error) => error.includes('sdk/typescript/README.md') && error.includes('undocumented')))
      assert.ok(!errors.some((error) => error.includes('sdk/typescript/README.md') && error.includes('"documented" is not documented')))
    },
  )
})

test('requires a substantive table for both API quick-reference contracts', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
      const readme = path.join(root, 'sdk', 'go', 'README.md')
      const source = fs.readFileSync(readme, 'utf8')
      fs.writeFileSync(
        readme,
        source
          .replace(/### 方法契约\n\n\| 方法 \| 参数 \| 说明 \|\n\| --- \| --- \| --- \|\n\| 示例 \| 参数 \| 方法说明 \|/, '### 方法契约\n\n只有说明，没有表格。')
          .replace(/### 字段契约\n\n\| 字段 \| 说明 \|\n\| --- \| --- \|\n\| 示例 \| 字段契约说明 \|/, '### 字段契约'),
      )
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('方法契约') && error.includes('表格')))
      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('字段契约') && error.includes('表格')))
    },
  )
})

test('requires parameter and meaning columns in API quick-reference contract tables', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
      const readme = path.join(root, 'sdk', 'go', 'README.md')
      const source = fs.readFileSync(readme, 'utf8')
      fs.writeFileSync(
        readme,
        source
          .replace('| 方法 | 参数 | 说明 |\n| --- | --- | --- |\n| 示例 | 参数 | 方法说明 |', '| 方法 | 说明 |\n| --- | --- |\n| 示例 | 方法说明 |')
          .replace('| 字段 | 说明 |\n| --- | --- |\n| 示例 | 字段契约说明 |', '| 字段 | 类型 |\n| --- | --- |\n| 示例 | string |'),
      )
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('方法契约') && error.includes('参数')))
      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('字段契约') && error.includes('含义')))
    },
  )
})

test('reports empty required cells in API quick-reference contract table rows', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
      const readme = path.join(root, 'sdk', 'go', 'README.md')
      const source = fs.readFileSync(readme, 'utf8')
      fs.writeFileSync(
        readme,
        source
          .replace('| 方法 | 参数 | 说明 |\n| --- | --- | --- |\n| 示例 | 参数 | 方法说明 |', '| 方法 | 参数 | 说明 |\n| --- | --- | --- |\n| 示例 | 参数 | |\n| 示例二 | | 另一个说明 |')
          .replace('| 字段 | 说明 |\n| --- | --- |\n| 示例 | 字段契约说明 |', '| 字段 | 说明 |\n| --- | --- |\n| 示例 | |\n| | 第二项说明 |'),
      )
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      for (const [subsection, row, column] of [
        ['方法契约', 'row 1', '说明'],
        ['方法契约', 'row 2', '参数'],
        ['字段契约', 'row 1', '说明'],
        ['字段契约', 'row 2', '字段'],
      ]) {
        assert.ok(errors.some((error) => (
          error.includes('sdk/go/README.md')
          && error.includes(subsection)
          && error.includes(row)
          && error.includes(column)
        )))
      }
    },
  )
})

test('does not treat a following table as data rows of the preceding contract table', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
      addToMethodContract(
        root,
        'go',
        '| 参数 | 含义 |\n| --- | --- |\n| `value` | 示例参数。 |\n\n| 方法 | 参数含义 | 返回 |\n| --- | --- | --- |\n| `example()` | 无参数。 | `None` |',
      )
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(!errors.some((error) => error.includes('sdk/go/README.md') && error.includes('empty required')))
    },
  )
})

test('requires every public client method in the 方法契约 subsection', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeEmptyClientSources(root)
      writeClientSource(
        root,
        'go',
        'package openviking\n\ntype Client struct{}\n\nfunc (c *Client) Documented() {}\n',
      )
      fs.appendFileSync(path.join(root, 'sdk', 'go', 'README.md'), '\n`Documented` 出现在方法契约之外。\n')
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.some((error) => error.includes('sdk/go/README.md') && error.includes('"Documented" is not documented')))
    },
  )
})

test('reports missing client source files and classes explicitly', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeClientSource(root, 'go', 'package openviking\n\ntype Client struct{}\n')
      writeClientSource(root, 'typescript', 'export class DifferentClient {}\n')
    },
    (root) => {
      const errors = checkSDKReadmes(root)

      assert.ok(errors.includes('sdk/python/openviking_sdk/client.py: required public client source file is missing'))
      assert.ok(errors.includes('sdk/typescript/src/client.ts: required public client class "OpenVikingClient" is missing'))
    },
  )
})

test('ignores non-public client members in Go, Python, and TypeScript', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      writeClientSource(
        root,
        'go',
        'package openviking\n\ntype Client struct{}\n\nfunc (c *Client) Documented() {}\nfunc (c *Client) hidden() {}\n',
      )
      writeClientSource(
        root,
        'python',
        'class SyncHTTPClient:\n    def documented(self):\n        pass\n\n    @property\n    def status(self):\n        return "ok"\n\n    def __init__(self):\n        pass\n\n    def _private(self):\n        pass\n',
      )
      writeClientSource(
        root,
        'typescript',
        'export class OpenVikingClient {\n  constructor() {}\n  documented() {}\n  private hidden() {}\n  protected guarded() {}\n}\n',
      )
      addToMethodContract(root, 'go', '`Documented`')
      addToMethodContract(root, 'python', '`documented`')
      addToMethodContract(root, 'typescript', '`documented`')
    },
    (root) => assert.deepEqual(checkSDKReadmes(root), []),
  )
})

test('reports any tracked-style Markdown documentation that links to a retired SDK README_CN.md', () => {
  withFixture(
    (root) => {
      for (const language of ['go', 'python', 'typescript']) writeReadme(root, language)
      const docs = path.join(root, 'examples', 'demo')
      fs.mkdirSync(docs, { recursive: true })
      fs.writeFileSync(
        path.join(docs, 'guide.md'),
        '[Go SDK](../../sdk/go/README_CN.md)\n',
      )
    },
    (root) => {
      const errors = checkSDKReadmes(root)
      assert.ok(errors.some((error) => error.includes('examples/demo/guide.md') && error.includes('README_CN.md')))
    },
  )
})
