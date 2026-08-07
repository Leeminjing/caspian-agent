# JavaScript (ECMAScript Date API) latest-stable

Source: https://github.com/tc39/ecma262/blob/main/spec.html

## Date.prototype.toISOString

### Description
Returns a string representation of the Date object in the ISO 8601 format (YYYY-MM-DDTHH:mm:ss.sssZ), using UTC.

### Method
`toISOString()`

### Parameters
None

### Request Example
```javascript
const myDate = new Date(Date.UTC(2023, 2, 15, 10, 30, 0));
console.log(myDate.toISOString());
```

### Response
#### Success Response (200)
- **Return Value** (String) - A string representing the Date in ISO 8601 format (e.g., "2023-03-15T10:30:00.000Z"). Throws a RangeError if the date is invalid or cannot be represented in the Date Time String Format.

#### Response Example
```javascript
"2023-03-15T10:30:00.000Z"
```
