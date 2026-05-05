const { spawn } = require(`child_process`)
const args = require(`options-to-args`)
const caffeinate = require(`./caffeinate`)

module.exports = caffeinate.bind(null, { spawn, args })
