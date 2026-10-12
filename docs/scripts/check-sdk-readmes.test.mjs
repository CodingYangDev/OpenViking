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

function withFixture(buildFixture, assertion) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'openviking-sdk-readmes-'))
  try {
    buildFixture(root)
    assertion(root)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

function writeReadme(root, language, sections = requiredSections) {
  const directory = path.join(root, 'sdk', language)
  fs.mkdirSync(directory, { recursive: true })
  fs.writeFileSync(
    path.join(directory, 'README.md'),
    `# OpenViking ${language} SDK\n\n这是中文 SDK 使用文档，用于说明安装、认证、任务、错误处理和兼容性。`.repeat(4) +
      `\n\n${sections.map((section) => `## ${section}`).join('\n\n')}\n`,
  )
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
