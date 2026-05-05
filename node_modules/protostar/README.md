# protostar

```javascript
const factory = require(`protostar`)

const changeName = (core, state, name) =>
  core(Object.assign({}, state, { name }))

const main = (core, state) => {
  console.log(state.name)
}

module.exports = factory.bind(
  null,
  main,
  { changeName },
  { name: `unnamed` }
)
```
