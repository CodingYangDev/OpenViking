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
const requiredContractSubsections = ['方法契约', '字段契约']

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

function pythonPublicMethods(classSource) {
  const methods = []
  let decorators = []
  for (const line of classSource.split('\n')) {
    if (/^    @/.test(line)) {
      decorators.push(line.trim())
      continue
    }
    const match = /^    (?:async )?def (\w+)\(/.exec(line)
    if (match) {
      if (!match[1].startsWith('_')
        && !decorators.some((decorator) => decorator.startsWith('@property'))) {
        methods.push(match[1])
      }
      decorators = []
      continue
    }
    if (line.trim()) decorators = []
  }
  return methods
}

function publicClientMethods(root, language) {
  if (language === 'go') {
    const directory = path.join(root, 'sdk', 'go')
    if (!fs.existsSync(directory)) return { methods: [] }
    return { methods: fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      if (!entry.isFile() || !entry.name.endsWith('.go')) return []
      return [...fs.readFileSync(path.join(directory, entry.name), 'utf8').matchAll(
        /^func \(\w+ \*?Client\) ([A-Z]\w*)\(/gm,
      )].map((match) => match[1])
    }) }
  }

  const sourceLabel = language === 'python'
    ? 'sdk/python/openviking_sdk/client.py'
    : 'sdk/typescript/src/client.ts'
  const sourceFile = path.join(
    root,
    'sdk',
    language,
    language === 'python' ? 'openviking_sdk/client.py' : 'src/client.ts',
  )
  if (!fs.existsSync(sourceFile)) {
    return { methods: [], error: `${sourceLabel}: required public client source file is missing` }
  }
  const source = fs.readFileSync(sourceFile, 'utf8')
  const className = language === 'python' ? 'SyncHTTPClient' : 'OpenVikingClient'
  const classStart = source.search(new RegExp(`^${language === 'typescript' ? 'export ' : ''}class ${className}\\b`, 'm'))
  if (classStart < 0) {
    return { methods: [], error: `${sourceLabel}: required public client class "${className}" is missing` }
  }
  const nextClass = source.slice(classStart + 1).search(/^(?:export )?class\s/m)
  const classSource = source.slice(classStart, nextClass < 0 ? undefined : classStart + 1 + nextClass)
  if (language === 'python') return { methods: pythonPublicMethods(classSource) }
  const methodPattern = /^  (?!(?:private|protected)\b)(?:public\s+)?(?:async\s+)?([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(/gm
  return { methods: [...classSource.matchAll(methodPattern)]
    .map((match) => match[1])
    .filter((method) => method !== 'constructor') }
}

function apiQuickReference(source) {
  const heading = /^##\s+API 速查表\s*$/m.exec(source)
  if (!heading) return ''
  const afterHeading = source.slice(heading.index + heading[0].length)
  const nextSection = afterHeading.search(/^##\s/m)
  return nextSection < 0 ? afterHeading : afterHeading.slice(0, nextSection)
}

function contractSubsection(quickReference, subsection) {
  const heading = new RegExp(`^###\\s+${subsection}\\s*$`, 'm').exec(quickReference)
  if (!heading) return null
  const afterHeading = quickReference.slice(heading.index + heading[0].length)
  const nextHeading = afterHeading.search(/^#{1,3}\s/m)
  return nextHeading < 0 ? afterHeading : afterHeading.slice(0, nextHeading)
}

function hasSubstantiveMarkdownTable(source) {
  return substantiveMarkdownTables(source).length > 0
}

function substantiveMarkdownTables(source) {
  return [...source.matchAll(/^(\|[^\n]+\|)[ \t]*\n\|[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|[ \t]*\n((?:\|[^\n]*\|[ \t]*(?:\n|$))*)/gm)]
    .filter((match) => match[2].trim())
    .map((match) => ({
      headers: match[1].split('|').slice(1, -1).map((header) => header.trim()),
      rows: match[2].trim().split('\n').map((row) => row.split('|').slice(1, -1).map((cell) => cell.trim())),
    }))
}

function hasTableHeaders(source, requiredHeaderGroups) {
  return substantiveMarkdownTables(source).some((table) => (
    requiredHeaderGroups.every((group) => group.some((header) => table.headers.includes(header)))
  ))
}

function emptyContractCells(source, requiredHeaders) {
  return substantiveMarkdownTables(source).flatMap((table) => {
    const columnIndexes = requiredHeaders.map((headers) => table.headers.findIndex((header) => headers.includes(header)))
    if (columnIndexes.some((index) => index < 0)) return []
    return table.rows.flatMap((row, rowIndex) => columnIndexes.flatMap((columnIndex) => (
      row[columnIndex] ? [] : [{ row: rowIndex + 1, column: table.headers[columnIndex] }]
    )))
  })
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
    const quickReference = apiQuickReference(source)
    for (const subsection of requiredContractSubsections) {
      const contract = contractSubsection(quickReference, subsection)
      if (contract === null) {
        errors.push(`${readmeLabel}: missing required API quick-reference subsection "${subsection}"`)
      } else if (!hasSubstantiveMarkdownTable(contract)) {
        errors.push(`${readmeLabel}: API quick-reference subsection "${subsection}" must contain a substantive Markdown 表格`)
      } else if (subsection === '方法契约' && !hasTableHeaders(contract, [['参数'], ['含义', '说明']])) {
        errors.push(`${readmeLabel}: API quick-reference subsection "方法契约" must contain a substantive Markdown 表格 with "参数" and "含义" or "说明" columns`)
      } else if (subsection === '字段契约' && !hasTableHeaders(contract, [['字段'], ['含义', '说明']])) {
        errors.push(`${readmeLabel}: API quick-reference subsection "字段契约" must contain a substantive Markdown 表格 with "字段" and "含义" or "说明" columns`)
      } else {
        const requiredHeaders = subsection === '方法契约'
          ? [['参数'], ['含义', '说明']]
          : [['字段'], ['含义', '说明']]
        for (const { row, column } of emptyContractCells(contract, requiredHeaders)) {
          errors.push(`${readmeLabel}: API quick-reference subsection "${subsection}" table row ${row} has an empty required "${column}" cell`)
        }
      }
    }
    const { methods, error } = publicClientMethods(root, language)
    if (error) errors.push(error)
    const methodContract = contractSubsection(quickReference, '方法契约') ?? ''
    for (const method of methods) {
      if (!new RegExp(`\\b${method}\\b`).test(methodContract)) {
        errors.push(`${readmeLabel}: public client method "${method}" is not documented`)
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
