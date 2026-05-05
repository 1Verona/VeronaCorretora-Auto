module.exports = ({ spawn, args }, options = {}) => {
  return new Promise((resolve, reject) => {
    const caffeinate = spawn(
      `caffeinate`,
      args
      .addAlias({ timeout: `t` })
      .addAlias({ watch: `w` })
      .addAlias({ pid: `w` })(options),
      {
        detached: true,
        stdio: `ignore`
      }
    ).on(`error`, reject)
    caffeinate.unref()
    process.nextTick(resolve.bind(null, caffeinate.pid))
  })
}
