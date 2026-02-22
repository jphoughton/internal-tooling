module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [2, 'always', [
      'feat', 'fix', 'docs', 'style', 'refactor',
      'perf', 'test', 'build', 'ci', 'chore', 'revert', 'wip'
    ]],
    'subject-max-length': [1, 'always', 100],
    'subject-case': [0],
  }
};
