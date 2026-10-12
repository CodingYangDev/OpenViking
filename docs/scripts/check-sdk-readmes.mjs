import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const sdkLanguages = ['go', 'python', 'typescript']
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

function markdownFiles(directory) {
  if (!fs.existsSync(directory)) return []
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const filename = path.join(directory, entry.name)
    if (entry.isDirectory()) return markdownFiles(filename)
    return entry.isFile() && entry.name.endsWith('.md') ? [filename] : []
  })
}

function trackedMarkdownFiles(root) {
  try {
    return execFileSync('git', ['-C', root, 'ls-files', '*.md'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .split('\n')
      .filter(Boolean)
      .map((filename) => path.join(root, filename))
  } catch {
    return markdownFiles(root)
  }
}

export function checkSDKReadmes(root) {
  const errors = []
  for (const language of sdkLanguages) {
    const directory = path.join(root, 'sdk', language)
    const readme = path.join(directory, 'README.md')
    const localizedReadme = path.join(directory, 'README_CN.md')
    const readmeLabel = `sdk/${language}/README.md`

    if (!fs.existsSync(readme)) {
      errors.push(`${readmeLabel}: required canonical SDK document is missing`)
      continue
    }
    if (fs.existsSync(localizedReadme)) {
      errors.push(`sdk/${language}/README_CN.md: duplicate SDK document; merge it into README.md and delete it`)
    }

    const source = fs.readFileSync(readme, 'utf8')
    const chineseCharacters = source.match(/[\u4e00-\u9fff]/g)?.length ?? 0
    if (chineseCharacters < 80) {
      errors.push(`${readmeLabel}: must use Chinese as its primary user-facing language`)
    }
    for (const section of requiredSections) {
      if (!new RegExp(`^#{2,3}\\s+${section}`, 'm').test(source)) {
        errors.push(`${readmeLabel}: missing required section "${section}"`)
      }
    }
  }

  for (const file of trackedMarkdownFiles(root)) {
    if (!fs.existsSync(file)) continue
    const source = fs.readFileSync(file, 'utf8')
    if (/sdk\/(?:go|python|typescript)\/README_CN\.md/.test(source)) {
      errors.push(
        `${path.relative(root, file)}: links to a retired SDK README_CN.md; use the canonical README.md`,
      )
    }
  }
  return errors
}

function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
  const errors = checkSDKReadmes(root)
  if (errors.length) {
    console.error(errors.join('\n'))
    process.exitCode = 1
    return
  }
  console.log('SDK README contract check passed: Go, Python, and TypeScript each have one complete Chinese README.md.')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main()
