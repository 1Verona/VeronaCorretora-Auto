# caffeinate

`caffeinate` is a system utility provided in macOS which prevents the system from sleeping indefinitely or on behalf of a utility.

## usage

```javascript
const caffeinate = require(`caffeinate`)

caffeinate({ pid: 45623 })
.then(caffeinatePid => console.log(caffeinatePid))
.catch(error => console.log(error))
```

## api

### `caffeinate([options])`

Spawns caffeinate. Resolves with the pid of the spawned caffeinate instance.

`options` can be any of those supported by caffeinate. For the sake of readability you can use `timeout` and `pid` in place of `t` and `w` respectively.

[options-to-args](https://www.npmjs.com/package/options-to-args) handles the conversion of options into spawn args.
